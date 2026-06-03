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
from mewcode.conversation import ConversationManager
from mewcode.agent import (
    Agent,
    ErrorEvent,
    LoopComplete,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    UsageEvent,
)
from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool
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
        # ch04: the ReAct Agent Loop drives multi-round tool use.
        self.agent = Agent(self.client, self.registry, config.protocol)

    async def _read_line(self, prompt: str) -> str:
        """Read a line without blocking the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, input, prompt)

    async def _stream_reply(self) -> None:
        """Drive the Agent Loop for one user turn and render its events."""
        printed_prefix = False
        in_thinking = False
        try:
            async for event in self.agent.run(self.conversation):
                if isinstance(event, ThinkingText):
                    if not in_thinking:
                        sys.stdout.write(f"{_GREEN}MewCode:{_RESET} {_DIM}[thinking] ")
                        in_thinking = True
                        printed_prefix = True
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                elif isinstance(event, StreamText):
                    if in_thinking:
                        sys.stdout.write(_RESET + "\n")
                        in_thinking = False
                        printed_prefix = False
                    if not printed_prefix:
                        sys.stdout.write(f"{_GREEN}MewCode:{_RESET} ")
                        printed_prefix = True
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                elif isinstance(event, ToolUseEvent):
                    if in_thinking:
                        sys.stdout.write(_RESET)
                        in_thinking = False
                    sys.stdout.write(f"\n{_DIM}[tool] {event.tool_name}…{_RESET}\n")
                    sys.stdout.flush()
                    printed_prefix = False
                elif isinstance(event, ToolResultEvent):
                    tag = (
                        f"{_YELLOW}error{_RESET}" if event.is_error
                        else f"{_GREEN}ok{_RESET}"
                    )
                    sys.stdout.write(f"{_DIM}  └ {event.tool_name} [{tag}{_DIM}]{_RESET}\n")
                    sys.stdout.flush()
                elif isinstance(event, RetryEvent):
                    sys.stdout.write(f"{_DIM}↻ retrying: {event.reason}{_RESET}\n")
                elif isinstance(event, UsageEvent):
                    self.total_input_tokens += event.input_tokens
                    self.total_output_tokens += event.output_tokens
                elif isinstance(event, LoopComplete):
                    if printed_prefix or in_thinking:
                        sys.stdout.write("\n")
                elif isinstance(event, ErrorEvent):
                    sys.stdout.write(f"\n{_YELLOW}[error] {event.message}{_RESET}\n")
        except asyncio.CancelledError:
            sys.stdout.write(f"\n{_YELLOW}[cancelled]{_RESET}\n")
            raise
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
            if user in ("/plan", "/plan on"):
                self.agent.set_plan_mode(True)
                print(f"{_DIM}plan mode ON — read-only; /do to execute.{_RESET}\n")
                continue
            if user in ("/plan off", "/do"):
                self.agent.set_plan_mode(False)
                print(f"{_DIM}plan mode OFF.{_RESET}\n")
                continue
            self.conversation.add_user_message(user)
            # Run the turn as a task so ctrl+c cancels the loop cleanly.
            task = asyncio.ensure_future(self._stream_reply())
            try:
                await task
            except asyncio.CancelledError:
                pass
            except KeyboardInterrupt:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass
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
