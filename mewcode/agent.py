"""Agent Loop — the ReAct driver (ch04).

One iteration = call LLM -> collect response -> if it asked for tools, execute
them and feed results back -> next iteration; no tool calls means done. The
loop is an async generator of `AgentEvent`s so the UI can `async for` over the
whole process.

ch04 runnable subset: multi-round loop, event stream, tool batching
(read-concurrent / write+command serial), max-iterations cap, max_tokens
escalation, Plan Mode interception, and cooperative cancellation. HITL
permissions, hooks, context compaction, memory, and teams are left as
extension points for later chapters.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mewcode.client import LLMClient, RateLimitError
from mewcode.config import MAX_TOKENS_CEILING
from mewcode.context import (
    CompactCircuitBreaker,
    CompactEvent,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    create_replacement_state,
    ensure_session_dir,
    estimate_conversation_tokens,
)
from mewcode.conversation import (
    ConversationManager,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from mewcode.prompts import (
    build_environment_context,
    build_plan_mode_reminder,
    build_system_prompt,
    is_plan_mode_reminder,
)
from mewcode.tools.base import (
    MAX_OUTPUT_CHARS,
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallStart,
)
from mewcode.tools.registry import ToolRegistry

MAX_OUTPUT_TOKENS_RECOVERIES = 3
CONSECUTIVE_UNKNOWN_LIMIT = 3
RATE_LIMIT_RETRIES = 3


# --------------------------------------------------------------------------- #
# Permission tri-state (extension point for ch06 HITL)
# --------------------------------------------------------------------------- #


class PermissionResponse(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"


# --------------------------------------------------------------------------- #
# Agent events
# --------------------------------------------------------------------------- #


@dataclass
class StreamText:
    text: str


@dataclass
class ThinkingText:
    text: str


@dataclass
class RetryEvent:
    reason: str


@dataclass
class ToolUseEvent:
    tool_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    tool_id: str
    tool_name: str
    output: str
    is_error: bool = False


@dataclass
class TurnComplete:
    iteration: int


@dataclass
class LoopComplete:
    stop_reason: str
    iterations: int


@dataclass
class UsageEvent:
    input_tokens: int
    output_tokens: int


@dataclass
class ErrorEvent:
    message: str


@dataclass
class CompactNotification:
    message: str


@dataclass
class SpillNotification:
    """ch08 Layer 1: tool results were written to disk and previewed."""

    count: int
    freed_chars: int


@dataclass
class HookEvent:
    name: str
    message: str


@dataclass
class PermissionRequest:
    tool_name: str
    arguments: dict[str, Any]
    future: "asyncio.Future[PermissionResponse]"


AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | UsageEvent
    | ErrorEvent
    | CompactNotification
    | SpillNotification
    | HookEvent
    | PermissionRequest
)


# --------------------------------------------------------------------------- #
# Stream collection
# --------------------------------------------------------------------------- #


@dataclass
class LLMResponse:
    """One assistant turn folded out of the raw stream events."""

    text: str = ""
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    tool_calls: list[ToolUseBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


class StreamCollector:
    """Folds a `StreamEvent` stream into an `LLMResponse`, yielding UI events."""

    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(self, stream) -> AsyncIterator[AgentEvent]:
        cur_thinking = ""
        async for event in stream:
            if isinstance(event, TextDelta):
                self.response.text += event.text
                yield StreamText(event.text)
            elif isinstance(event, ThinkingDelta):
                cur_thinking += event.text
                yield ThinkingText(event.text)
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(
                    ThinkingBlock(event.thinking or cur_thinking, event.signature)
                )
                cur_thinking = ""
            elif isinstance(event, ToolCallStart):
                pass  # start is informational; complete carries the args
            elif isinstance(event, ToolCallComplete):
                block = ToolUseBlock(event.tool_id, event.tool_name, event.arguments)
                self.response.tool_calls.append(block)
                yield ToolUseEvent(event.tool_id, event.tool_name, event.arguments)
            elif isinstance(event, StreamEnd):
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens


# --------------------------------------------------------------------------- #
# Tool batching
# --------------------------------------------------------------------------- #


@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolUseBlock]


def partition_tool_calls(
    tool_calls: list[ToolUseBlock], registry: ToolRegistry
) -> list[ToolBatch]:
    """Group consecutive concurrency-safe calls; others get their own batch.

    Read-only tools (`is_concurrency_safe`) run together; write and command
    tools each run serially.
    """
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        safe = False
        if registry.is_enabled(tc.name):
            try:
                safe = registry.get(tc.name).is_concurrency_safe
            except KeyError:
                safe = False
        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


@dataclass
class _ToolExecResult:
    block: ToolResultBlock
    tool_name: str


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


_ADJECTIVES = [
    "amber", "brisk", "calm", "deft", "eager", "fleet", "gentle", "honest",
    "ivory", "jolly", "keen", "lucid", "merry", "noble", "olive", "prime",
    "quiet", "rapid", "sage", "tidy", "umber", "vivid", "warm", "zesty",
]
_NOUNS = [
    "atlas", "beacon", "cedar", "delta", "ember", "falcon", "grove", "harbor",
    "iris", "jade", "kite", "lumen", "maple", "nova", "orbit", "pearl",
    "quartz", "river", "spruce", "talon", "umbra", "vertex", "willow", "zephyr",
]


class Agent:
    """Drives the ReAct loop and yields AgentEvents."""

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        *,
        work_dir: str = ".",
        max_iterations: int = 50,
        plan_mode: bool = False,
        coordinator_mode: bool = False,
        permission_checker: Any = None,
        ask_permission: Any = None,
        context_window: int = 200_000,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.plan_mode = plan_mode
        self.coordinator_mode = coordinator_mode
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._plan_path_cache: str | None = None
        # ch08: two-layer context management.
        self.context_window = context_window
        self.session_dir = ensure_session_dir(work_dir)
        self.replacement_state = create_replacement_state()
        self.recovery_state = RecoveryState()
        self.compact_breaker = CompactCircuitBreaker()
        # ch06: permission system. `permission_checker.check(tool, args)` returns
        # a Decision; on "ask" we call the async `ask_permission(tool, args)`
        # callback (set by the UI) to get a PermissionResponse.
        self.permission_checker = permission_checker
        self.ask_permission = ask_permission

    # --- plan mode ------------------------------------------------------- #

    def set_plan_mode(self, on: bool) -> None:
        self.plan_mode = on
        # Keep the permission mode in sync so `/mode` reflects plan state (ch06).
        if self.permission_checker is not None:
            from mewcode.permissions import PermissionMode

            self.permission_checker.mode = (
                PermissionMode.PLAN if on else PermissionMode.DEFAULT
            )

    def set_permission_mode(self, mode) -> None:
        """Update the permission checker's mode (ch06)."""
        if self.permission_checker is not None:
            self.permission_checker.mode = mode
        # PLAN is also the ch04 plan flag; keep them consistent.
        from mewcode.permissions import PermissionMode

        self.plan_mode = mode == PermissionMode.PLAN

    def _get_plan_path(self) -> str:
        """Lazily generate a readable, single-instance plan file path."""
        if self._plan_path_cache is None:
            slug = (
                f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-"
                f"{datetime.now().strftime('%m%d-%H%M')}"
            )
            plans_dir = Path(self.work_dir) / ".mewcode" / "plans"
            plans_dir.mkdir(parents=True, exist_ok=True)
            self._plan_path_cache = str(plans_dir / f"{slug}.md")
        return self._plan_path_cache

    def _is_plan_target(self, tool, args: dict[str, Any]) -> bool:
        """True if this write targets the plan file (allowed even in plan mode)."""
        if tool.name not in ("WriteFile", "EditFile"):
            return False
        target = str(args.get("file_path") or args.get("path") or "")
        if not target:
            return False
        norm = target.replace("\\", "/")
        if ".mewcode/plans/" in norm:
            return True
        cached = self._plan_path_cache
        return bool(cached) and Path(target).name == Path(cached).name

    # --- main loop ------------------------------------------------------- #

    async def run(
        self, conversation: ConversationManager
    ) -> AsyncIterator[AgentEvent]:
        iteration = 0
        consecutive_unknown = 0
        recoveries = 0

        # Inject environment context once, on the user channel (ch05). Stays
        # out of the cacheable system prompt; idempotent via env_injected.
        conversation.inject_environment(build_environment_context(self.work_dir))

        while True:
            iteration += 1
            if iteration > self.max_iterations:
                yield ErrorEvent(f"Reached max iterations ({self.max_iterations})")
                return

            # ch08 Layer 2: auto-compact when history nears the window limit.
            # Estimate the prompt size up front (tiktoken) so we compact BEFORE
            # sending an oversized request, not just after usage is reported.
            schemas = self.registry.get_all_schemas(self.protocol)
            estimated = estimate_conversation_tokens(conversation, tools=schemas)
            compact = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=schemas,
                estimated_tokens=estimated,
            )
            if isinstance(compact, CompactEvent):
                after = estimate_conversation_tokens(conversation, tools=schemas)
                yield CompactNotification(
                    f"Compacted: {compact.before_tokens} → {after} estimated tokens"
                )
                conversation.inject_environment(
                    build_environment_context(self.work_dir)
                )

            if self.plan_mode:
                plan_path = self._get_plan_path()
                if self.permission_checker is not None:
                    self.permission_checker.plan_file_path = plan_path
                reminder = build_plan_mode_reminder(
                    plan_path, Path(plan_path).exists(), iteration
                )
                # plan reminder goes through the user channel as a
                # <system-reminder>, not into the system prompt
                conversation.add_system_reminder(reminder)
            else:
                # Not in plan mode: drop any stale plan-mode reminders left in
                # history, so the model stops obeying "MUST NOT run tools" after
                # the user switched back to a normal mode (e.g. default).
                _purge_plan_reminders(conversation)

            system = build_system_prompt(
                coordinator_mode=self.coordinator_mode, work_dir=self.work_dir
            )
            tools = self.registry.get_all_schemas(self.protocol)

            # ch08 Layer 1: spill oversized tool results just before the API
            # call (byte-stable previews come back from replacement_state).
            api_conv, new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            append_replacement_records(self.session_dir, new_records)
            if new_records:
                freed = _freed_chars(conversation, new_records)
                yield SpillNotification(len(new_records), freed)

            # Stream the turn, retrying on rate limits (429). A TPM rate limit
            # is rejected before any tokens arrive, so re-running the whole
            # stream is safe as long as nothing was emitted yet.
            attempt = 0
            while True:
                collector = StreamCollector()
                try:
                    async for ev in collector.consume(
                        self.client.stream(api_conv, system=system, tools=tools)
                    ):
                        yield ev
                    break
                except RateLimitError as e:
                    produced = (
                        collector.response.text or collector.response.tool_calls
                    )
                    if produced or attempt >= RATE_LIMIT_RETRIES:
                        yield ErrorEvent(f"Rate limited: {e}")
                        return
                    attempt += 1
                    wait = (
                        e.retry_after
                        if e.retry_after
                        else min(2**attempt * 2, 30)
                    )
                    yield RetryEvent(
                        reason=f"rate limited; retrying in {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
            resp = collector.response

            # ch08: real usage drives the Layer 2 trigger next turn.
            conversation.last_input_tokens = resp.input_tokens
            self.total_input_tokens += resp.input_tokens
            self.total_output_tokens += resp.output_tokens
            yield UsageEvent(resp.input_tokens, resp.output_tokens)

            # max_tokens escalation / recovery
            if resp.stop_reason == "max_tokens" and recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
                recoveries += 1
                self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                conversation.add_assistant_message(
                    resp.text, thinking_blocks=resp.thinking_blocks
                )
                conversation.add_user_message("Continue from where you left off.")
                yield RetryEvent(reason="max_tokens escalation")
                continue

            # no tool calls -> done
            if not resp.tool_calls:
                conversation.add_assistant_message(
                    resp.text, thinking_blocks=resp.thinking_blocks
                )
                yield LoopComplete(stop_reason="end_turn", iterations=iteration)
                return

            # write assistant message with its tool_uses
            conversation.add_assistant_message(
                resp.text,
                tool_uses=resp.tool_calls,
                thinking_blocks=resp.thinking_blocks,
            )

            # track unknown tools
            if all(not self.registry.is_enabled(tc.name) for tc in resp.tool_calls):
                consecutive_unknown += 1
                if consecutive_unknown >= CONSECUTIVE_UNKNOWN_LIMIT:
                    yield ErrorEvent("Too many consecutive unknown tool calls")
                    return
            else:
                consecutive_unknown = 0

            # execute, batch by category
            results: list[ToolResultBlock] = []
            for batch in partition_tool_calls(resp.tool_calls, self.registry):
                if batch.concurrent and len(batch.calls) > 1:
                    execs = await asyncio.gather(
                        *(self._execute_one(tc) for tc in batch.calls)
                    )
                else:
                    execs = [await self._execute_one(tc) for tc in batch.calls]
                for ex in execs:
                    results.append(ex.block)
                    yield ToolResultEvent(
                        ex.block.tool_use_id,
                        ex.tool_name,
                        ex.block.content,
                        ex.block.is_error,
                    )

            conversation.add_tool_results_message(results)
            yield TurnComplete(iteration=iteration)

    # --- tool execution -------------------------------------------------- #

    async def _execute_one(self, tc: ToolUseBlock) -> _ToolExecResult:
        """Execute a single tool call into a structured result (never raises)."""
        # unknown / disabled
        if not self.registry.is_enabled(tc.name):
            return _ToolExecResult(
                ToolResultBlock(tc.id, f"Unknown tool: {tc.name}", is_error=True),
                tc.name,
            )
        tool = self.registry.get(tc.name)

        # Plan Mode fallback block — only when there is NO permission checker.
        # The checker handles plan mode itself (including the plan-file
        # exemption), so we must not pre-empt it here, otherwise even writing
        # the plan document would be blocked.
        if (
            self.plan_mode
            and self.permission_checker is None
            and tool.category in {"write", "command"}
            and not self._is_plan_target(tool, tc.input)
        ):
            return _ToolExecResult(
                ToolResultBlock(
                    tc.id,
                    f"{tc.name} is unavailable in PLAN mode. Run /do to exit "
                    "plan mode before making changes.",
                    is_error=True,
                ),
                tc.name,
            )

        # Permission check (ch06): blacklist / sandbox / rules / mode.
        if self.permission_checker is not None:
            denied = await self._check_permission(tool, tc)
            if denied is not None:
                return denied

        try:
            params = tool.params_model.model_validate(tc.input)
        except ValidationError as e:
            return _ToolExecResult(
                ToolResultBlock(tc.id, f"Parameter validation error: {e}", is_error=True),
                tc.name,
            )

        try:
            result = await tool.execute(params)
            output, is_error = result.output, result.is_error
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — tool failures must not crash loop
            output, is_error = f"Tool execution error: {e}", True

        # ch08: snapshot ReadFile content for post-compact recovery.
        self._snapshot_for_recovery(tc, is_error)

        output = _maybe_truncate(output)
        return _ToolExecResult(
            ToolResultBlock(tc.id, output, is_error=is_error), tc.name
        )

    def _snapshot_for_recovery(self, tc: ToolUseBlock, is_error: bool) -> None:
        """Record a ReadFile's bytes so recovery can re-attach it post-compact."""
        if is_error or tc.name != "ReadFile":
            return
        path = str(tc.input.get("file_path") or tc.input.get("path") or "")
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return
        self.recovery_state.record_file_read(path, content)

    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactEvent | str:
        """Force a Layer 2 compaction (the /compact command). Returns event or error."""
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self.registry.get_all_schemas(self.protocol),
        )
        if result is None:
            if not conversation.history:
                return "对话为空，无需压缩。"
            return "无需压缩：当前对话量未超过保留预算，全部作为近期原文保留。"
        return result

    async def _check_permission(self, tool, tc: ToolUseBlock):
        """Run the permission check. Returns an error result if denied, else None.

        On `ask`, calls the UI's `ask_permission` callback; `ALLOW_ALWAYS` is
        self-learned into a local rule.
        """
        decision = self.permission_checker.check(tool, tc.input)
        if decision.effect == "allow":
            return None
        if decision.effect == "deny":
            return _ToolExecResult(
                ToolResultBlock(
                    tc.id, f"Permission denied: {decision.reason}", is_error=True
                ),
                tc.name,
            )
        # ask -> hand off to the UI
        if self.ask_permission is None:
            return _ToolExecResult(
                ToolResultBlock(
                    tc.id,
                    f"Permission required for {tool.name} but no approver is "
                    "configured; denied.",
                    is_error=True,
                ),
                tc.name,
            )
        response = await self.ask_permission(tool, tc.input)
        if response == PermissionResponse.DENY:
            return _ToolExecResult(
                ToolResultBlock(tc.id, "Permission denied by user", is_error=True),
                tc.name,
            )
        if response == PermissionResponse.ALLOW_ALWAYS:
            self._self_learn_rule(tool, tc.input)
        return None

    def _self_learn_rule(self, tool, args: dict[str, Any]) -> None:
        """Persist an allow rule for this tool+pattern (ALLOW_ALWAYS)."""
        from mewcode.permissions import Rule, extract_content

        engine = getattr(self.permission_checker, "rule_engine", None)
        if engine is None:
            return
        content = extract_content(tool.name, args)
        pattern = (content[:60] + "*") if content else "*"
        engine.append_local_rule(Rule(tool.name, pattern, "allow"))


def _maybe_truncate(output: str) -> str:
    """Cap a single tool result so it can't blow up the next request."""
    if len(output) > MAX_OUTPUT_CHARS:
        return output[:MAX_OUTPUT_CHARS] + "\n… (output truncated)"
    return output
def _purge_plan_reminders(conversation: ConversationManager) -> None:
    """Remove stale Plan Mode reminder messages from history (in place).

    These are standalone `<system-reminder>` user messages with no tool blocks,
    so dropping them never breaks tool_use/tool_result pairing.
    """
    conversation.history = [
        m
        for m in conversation.history
        if not (
            m.role == "user"
            and not m.tool_results
            and not m.tool_uses
            and is_plan_mode_reminder(m.content)
        )
    ]


def _freed_chars(conversation: ConversationManager, records: list) -> int:
    """Chars saved by Layer 1 = original tool-result length − preview length."""
    orig: dict[str, int] = {}
    for msg in conversation.history:
        for tr in msg.tool_results:
            orig[tr.tool_use_id] = len(tr.content)
    freed = 0
    for r in records:
        before = orig.get(r.tool_use_id, len(r.replacement))
        freed += max(0, before - len(r.replacement))
    return freed
