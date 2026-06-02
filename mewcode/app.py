"""MewCode interactive terminal app.

A minimal TUI: read a line from the user, stream the model's reply token by
token, remember the whole conversation across turns. Pure chat — no tools, no
file edits. Works with either provider via a YAML config (see config.py).

Run with:  python -m mewcode [path/to/config.yaml]
"""

from __future__ import annotations

import asyncio
import sys

from mewcode.client import LLMClient, LLMError, create_client
from mewcode.config import ProviderConfig, load_config
from mewcode.conversation import ConversationManager, ThinkingBlock
from mewcode.tools.base import (
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
)

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

    async def _read_line(self, prompt: str) -> str:
        """Read a line without blocking the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, input, prompt)

    async def _stream_reply(self) -> None:
        """Stream one assistant turn and write it back to the conversation."""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_blocks: list[ThinkingBlock] = []
        in_thinking = False

        sys.stdout.write(f"{_GREEN}MewCode:{_RESET} ")
        sys.stdout.flush()

        try:
            async for event in self.client.stream(
                self.conversation, system=self.system, tools=[]
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
                elif isinstance(event, StreamEnd):
                    self.total_input_tokens += event.input_tokens
                    self.total_output_tokens += event.output_tokens
        except LLMError as e:
            sys.stdout.write(f"\n{_YELLOW}[error] {e}{_RESET}\n")
            return

        sys.stdout.write("\n")
        # If thinking streamed deltas but no explicit Complete arrived, keep it.
        if thinking_parts and not thinking_blocks:
            thinking_blocks.append(ThinkingBlock("".join(thinking_parts)))

        self.conversation.add_assistant_message(
            "".join(text_parts), thinking_blocks=thinking_blocks
        )

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
