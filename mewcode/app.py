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
    PermissionResponse,
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
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
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
        # Tool system (ch03): six core tools + ToolSearch + AskUserQuestion.
        self.file_cache = FileCache()
        self.registry = create_default_registry(file_cache=self.file_cache)
        self.registry.register(
            ToolSearchTool(self.registry, protocol=config.protocol)
        )
        self.registry.register(AskUserTool())
        # ch06: permission system (defense in depth).
        import os
        work_dir = os.getcwd()
        home = os.path.expanduser("~")
        self.permission_checker = PermissionChecker(
            DangerousCommandDetector(),
            PathSandbox(work_dir),
            RuleEngine(
                user_rules_path=os.path.join(home, ".mewcode", "permissions.yaml"),
                project_rules_path=os.path.join(
                    work_dir, ".mewcode", "permissions.yaml"
                ),
                local_rules_path=os.path.join(
                    work_dir, ".mewcode", "permissions.local.yaml"
                ),
            ),
            mode=PermissionMode.DEFAULT,
        )
        # ch04: the ReAct Agent Loop drives multi-round tool use.
        self.agent = Agent(
            self.client,
            self.registry,
            config.protocol,
            permission_checker=self.permission_checker,
            ask_permission=self._ask_permission,
        )
        # ch07: external MCP servers (connected lazily at run() start).
        self._mcp_server_configs = list(getattr(config, "mcp_servers", []) or [])
        self.mcp_manager = None

    async def _read_line(self, prompt: str) -> str:
        """Read a line without blocking the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, input, prompt)

    async def _ask_permission(self, tool, args) -> PermissionResponse:
        """HITL: prompt the user to approve a tool call (ch06)."""
        from mewcode.permissions import extract_content

        summary = extract_content(tool.name, args) or "(no args)"
        sys.stdout.write(
            f"\n{_YELLOW}Permission needed{_RESET} for {_CYAN}{tool.name}{_RESET}: "
            f"{summary[:80]}\n"
            f"{_DIM}  [y] allow once  [A] allow always  [n] deny{_RESET}\n"
        )
        choice = (await self._read_line(f"{_CYAN}approve? ›{_RESET} ")).strip()
        if choice == "y":
            return PermissionResponse.ALLOW
        if choice == "A":
            return PermissionResponse.ALLOW_ALWAYS
        return PermissionResponse.DENY

    _MODE_HELP = {
        PermissionMode.DEFAULT: "read allow, write/command ask",
        PermissionMode.ACCEPT_EDITS: "writes allow, command ask",
        PermissionMode.PLAN: "read-only (no writes/commands)",
        PermissionMode.BYPASS: "allow all except the hard floor",
        PermissionMode.DONT_ASK: "never prompt (allow), hard floor still applies",
        PermissionMode.CUSTOM: "rules-driven, otherwise ask",
    }

    # accepted spellings for each permission mode (normalized: lowercase,
    # no '-' / '_'). Used by both `/mode <name>` and the direct `/<name>` cmds.
    _MODE_ALIASES = {
        "default": PermissionMode.DEFAULT,
        "acceptedits": PermissionMode.ACCEPT_EDITS,
        "accept": PermissionMode.ACCEPT_EDITS,
        "plan": PermissionMode.PLAN,
        "bypass": PermissionMode.BYPASS,
        "bypasspermissions": PermissionMode.BYPASS,
        "yolo": PermissionMode.BYPASS,
        "dontask": PermissionMode.DONT_ASK,
        "custom": PermissionMode.CUSTOM,
    }

    @staticmethod
    def _normalize_mode_token(tok: str) -> str:
        return tok.lower().lstrip("/").replace("-", "").replace("_", "")

    def _set_mode(self, mode: PermissionMode) -> None:
        """Switch the permission mode, keeping the plan flag in sync (ch06)."""
        was_plan = self.agent.plan_mode
        if mode is PermissionMode.PLAN:
            self.agent.set_plan_mode(True)
        else:
            self.agent.set_plan_mode(False)
            self.permission_checker.mode = mode
            if was_plan:
                self.conversation.add_system_reminder(
                    "Plan mode is now OFF. You may make changes again: "
                    "WriteFile, EditFile, and Bash are available."
                )
        print(f"{_DIM}permission mode → {mode.value}{_RESET}\n")

    def _print_modes(self) -> None:
        current = self.permission_checker.mode
        print(f"\n{_DIM}current permission mode:{_RESET} "
              f"{_CYAN}{current.value}{_RESET}")
        print(f"{_DIM}available modes:{_RESET}")
        for m, desc in self._MODE_HELP.items():
            mark = f"{_GREEN}●{_RESET}" if m is current else f"{_DIM}○{_RESET}"
            print(f"  {mark} {m.value:<18}{_DIM}{desc}{_RESET}")
        print(f"{_DIM}switch with: /mode <name>  or  /default /acceptEdits "
              f"/plan /bypassPermissions /dontAsk{_RESET}\n")

    def _handle_mode(self, user: str) -> None:
        """`/mode [name]` — show current/all modes, or switch (ch06)."""
        parts = user.split()
        if len(parts) == 1:
            self._print_modes()
            return
        mode = self._MODE_ALIASES.get(self._normalize_mode_token(parts[1]))
        if mode is None:
            print(f"{_YELLOW}unknown mode: {parts[1]}{_RESET}\n")
            return
        self._set_mode(mode)

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

    async def _init_mcp(self) -> None:
        """Connect configured MCP servers and register their tools (ch07)."""
        if not self._mcp_server_configs:
            return
        from mewcode.mcp import MCPManager

        self.mcp_manager = MCPManager()
        self.mcp_manager.load_configs(self._mcp_server_configs)
        n = len(self._mcp_server_configs)
        print(f"{_DIM}Connecting to {n} MCP server(s)…{_RESET}")
        errors = await self.mcp_manager.register_all_tools(self.registry)
        mcp_tools = [
            t for t in self.registry.list_tools() if t.name.startswith("mcp_")
        ]
        connected = n - len(errors)
        print(
            f"{_DIM}Connected to {connected} MCP server(s), "
            f"{len(mcp_tools)} tools registered.{_RESET}"
        )
        for err in errors:
            print(f"{_YELLOW}  MCP server failed — {err}{_RESET}")
        if mcp_tools:
            listing = ", ".join(t.name for t in mcp_tools)
            self.conversation.add_system_reminder(
                "External MCP tools are available (discover them via ToolSearch "
                f"before use): {listing}"
            )
        print()

    async def _shutdown_mcp(self) -> None:
        """Tear down all MCP connections (ch07). Idempotent."""
        if self.mcp_manager is not None:
            await self.mcp_manager.shutdown()
            self.mcp_manager = None

    async def run(self) -> None:
        print(_BANNER)
        print(
            f"{_DIM}provider={self.config.name} protocol={self.config.protocol} "
            f"model={self.config.model} thinking={self.config.thinking}{_RESET}\n"
        )
        await self._init_mcp()
        try:
            await self._run_loop()
        finally:
            await self._shutdown_mcp()

    async def _run_loop(self) -> None:
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
                was_plan = self.agent.plan_mode
                self.agent.set_plan_mode(False)
                if was_plan:
                    # Tell the model plan mode ended so it stops obeying the
                    # earlier "Plan mode is active" reminders still in history.
                    self.conversation.add_system_reminder(
                        "Plan mode is now OFF. You may make changes again: "
                        "WriteFile, EditFile, and Bash are available. Proceed "
                        "with the user's request."
                    )
                print(f"{_DIM}plan mode OFF.{_RESET}\n")
                continue
            if user.startswith("/mode"):
                self._handle_mode(user)
                continue
            # Direct per-mode commands: /default /acceptEdits /bypassPermissions
            # /dontAsk /custom  (/plan and /do are handled above).
            if user.startswith("/") and " " not in user:
                token = self._normalize_mode_token(user)
                if token in self._MODE_ALIASES and token != "plan":
                    self._set_mode(self._MODE_ALIASES[token])
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
