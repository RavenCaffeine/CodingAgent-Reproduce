"""MewCode interactive terminal app.

A minimal TUI: read a line from the user, stream the model's reply token by
token, remember the whole conversation across turns, and run tools (read /
write / edit files, shell, glob, grep) in a single round. Works with any
provider via a YAML config (see config.py).

Run with:  python -m mewcode [path/to/config.yaml]
"""

from __future__ import annotations

import asyncio
import sys

from mewcode.client import LLMClient, LLMError, create_client
from mewcode.config import ProviderConfig, load_config
from mewcode.cache import FileCache
from mewcode.conversation import (
    ConversationManager,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.base import (
    MAX_OUTPUT_CHARS,
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallStart,
)
from mewcode.tools.impl.tool_search import ToolSearchTool

# ANSI styling (kept tiny; degrades to plain text in dumb terminals).
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

_BANNER = f"""{_CYAN}
 __  __                 ____          _
|  \\/  | _____      __ / ___|___   __| | ___
| |\\/| |/ _ \\ \\ /\\ / /| |   / _ \\ / _` |/ _ \\
| |  | |  __/\\ V  V / | |__| (_) | (_| |  __/
|_|  |_|\\___| \\_/\\_/   \\____\\___/ \\__,_|\\___|
{_RESET}  terminal AI assistant — type your message, /exit to quit
"""


class MewCodeApp:
    """Owns the client, the conversation, and the read/stream/render loop."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.client: LLMClient = create_client(config)
        self.conversation = ConversationManager()
        self.system = "You are MewCode, a helpful terminal AI assistant."
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # Tool system (ch03): six core tools + ToolSearch + AskUserQuestion.
        self.file_cache = FileCache()
        self.registry = create_default_registry(file_cache=self.file_cache)
        self.registry.register(
            ToolSearchTool(self.registry, protocol=config.protocol)
        )
        self.registry.register(AskUserTool())

    async def _read_line(self, prompt: str) -> str:
        """Read a line without blocking the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, input, prompt)

    async def _consume(self, tools: list) -> tuple[str, list[ThinkingBlock], list[ToolUseBlock]]:
        """Stream one assistant turn, render it, and write it to history.

        Returns the text, thinking blocks, and any tool calls the model made.
        """
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_blocks: list[ThinkingBlock] = []
        tool_uses: list[ToolUseBlock] = []
        in_thinking = False

        sys.stdout.write(f"{_GREEN}MewCode:{_RESET} ")
        sys.stdout.flush()

        async for event in self.client.stream(
            self.conversation, system=self.system, tools=tools
        ):
            if isinstance(event, ThinkingDelta):
                if not in_thinking:
                    sys.stdout.write(f"\n{_DIM}[thinking] ")
                    in_thinking = True
                sys.stdout.write(event.text)
                sys.stdout.flush()
                thinking_parts.append(event.text)
            elif isinstance(event, ThinkingComplete):
                if in_thinking:
                    sys.stdout.write(f"{_RESET}\n{_GREEN}MewCode:{_RESET} ")
                    in_thinking = False
                thinking_blocks.append(
                    ThinkingBlock(event.thinking, event.signature)
                )
            elif isinstance(event, TextDelta):
                sys.stdout.write(event.text)
                sys.stdout.flush()
                text_parts.append(event.text)
            elif isinstance(event, ToolCallStart):
                sys.stdout.write(f"\n{_DIM}[tool] {event.tool_name}...{_RESET}\n")
                sys.stdout.flush()
            elif isinstance(event, ToolCallComplete):
                tool_uses.append(
                    ToolUseBlock(event.tool_id, event.tool_name, event.arguments)
                )
            elif isinstance(event, StreamEnd):
                self.total_input_tokens += event.input_tokens
                self.total_output_tokens += event.output_tokens

        sys.stdout.write("\n")
        if thinking_parts and not thinking_blocks:
            thinking_blocks.append(ThinkingBlock("".join(thinking_parts)))

        self.conversation.add_assistant_message(
            "".join(text_parts), tool_uses=tool_uses, thinking_blocks=thinking_blocks
        )
        return "".join(text_parts), thinking_blocks, tool_uses

    async def _execute_tools(self, tool_uses: list[ToolUseBlock]) -> None:
        """Run each tool call, render it, and feed results back into history."""
        results: list[ToolResultBlock] = []
        for tu in tool_uses:
            try:
                tool = self.registry.get(tu.name)
                params = tool.params_model.model_validate(tu.input)
                result = await tool.execute(params)
                output = result.output
                is_error = result.is_error
            except KeyError:
                output, is_error = f"Unknown tool: {tu.name}", True
            except Exception as e:  # noqa: BLE001 — never crash the loop
                output, is_error = f"Tool error: {e}", True

            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
            tag = f"{_YELLOW}error{_RESET}" if is_error else f"{_GREEN}ok{_RESET}"
            sys.stdout.write(f"{_DIM}  └ {tu.name} [{tag}{_DIM}]{_RESET}\n")
            results.append(ToolResultBlock(tu.id, output, is_error=is_error))

        self.conversation.add_tool_results_message(results)

    async def _stream_reply(self) -> None:
        """Handle one user turn: stream, run tools once, stream the answer.

        Single round (ch03): the model may call tools once; we execute them,
        feed results back, and stream the final answer. The auto Agent Loop is
        ch04, so we do not iterate further even if more tools are requested.
        """
        tools = self.registry.get_all_schemas(self.config.protocol)
        try:
            _, _, tool_uses = await self._consume(tools)
            if tool_uses:
                await self._execute_tools(tool_uses)
                await self._consume(tools)
        except LLMError as e:
            sys.stdout.write(f"\n{_YELLOW}[error] {e}{_RESET}\n")

    async def run(self) -> None:
        print(_BANNER)
        print(
            f"{_DIM}provider={self.config.name} protocol={self.config.protocol} "
            f"model={self.config.model} thinking={self.config.thinking}{_RESET}\n"
        )
        while True:
            try:
                user = (await self._read_line(f"{_CYAN}you ›{_RESET} ")).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye.")
                return
            if not user:
                continue
            if user in ("/exit", "/quit", ":q"):
                print(
                    f"{_DIM}tokens — in:{self.total_input_tokens} "
                    f"out:{self.total_output_tokens}{_RESET}\nbye."
                )
                return
            self.conversation.add_user_message(user)
            await self._stream_reply()
            print()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = argv[0] if argv else "config.yaml"
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(
            f"{_YELLOW}No config at '{config_path}'. "
            f"Create one (see config.example.yaml).{_RESET}"
        )
        return 1
    except ValueError as e:
        print(f"{_YELLOW}Config error: {e}{_RESET}")
        return 1

    try:
        app = MewCodeApp(config)
    except LLMError as e:
        print(f"{_YELLOW}{e}{_RESET}")
        return 1

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
