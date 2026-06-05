"""Two-layer context management + post-compact recovery (ch08).

Layer 1 (cheap, no LLM): `apply_tool_result_budget` spills oversized tool
results to disk and replaces them with a byte-stable `<persisted-output>`
preview. A cross-turn `ContentReplacementState` records each decision once so
later turns re-read identical bytes (required for Anthropic prompt-cache hits).

Layer 2 (expensive, LLM): `auto_compact` summarizes the OLDER part of the
conversation when `last_input_tokens` crosses a threshold, keeping the recent
tail verbatim (KEEP_RECENT_TOKENS / MIN_KEEP_MESSAGES / KEEP_MAX_TOKENS). New
history = `[摘要] + 边界消息 + 近期原文`. A `CompactCircuitBreaker` trips after
repeated failures. After summarizing, `build_recovery_attachment` re-attaches
recently-read files / activated skills / available tools so the model doesn't
lose working memory.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from mewcode.conversation import ConversationManager, Message, ToolResultBlock

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SINGLE_RESULT_CHAR_LIMIT = 50_000
AGGREGATE_CHAR_LIMIT = 2_0000
PREVIEW_CHARS = 2_000
KEEP_RECENT_TURNS = 10
OLD_RESULT_SNIP_CHARS = 2_000
SNIPPED_TAG = "<snipped>"
PERSISTED_TAG = "<persisted-output>"

SUMMARY_OUTPUT_RESERVE = 20_000
AUTO_COMPACT_SAFETY_MARGIN = 13_000
MANUAL_COMPACT_SAFETY_MARGIN = 3_000

# Layer 2 keep-recent tail: how much of the newest conversation to keep
# verbatim instead of folding into the summary.
KEEP_RECENT_TOKENS = 50_000
MIN_KEEP_MESSAGES = 5
KEEP_MAX_TOKENS = 40_000

SESSION_SUBDIR = ".mewcode/session/tool-results"
REPLACEMENT_RECORDS_FILENAME = "replacement_records.jsonl"

# Post-compact recovery state
RECOVERY_FILE_LIMIT = 5
RECOVERY_TOKENS_PER_FILE = 5_000
RECOVERY_SKILLS_BUDGET = 25_000
RECOVERY_TOKENS_PER_SKILL = 5_000
_RECOVERY_CHARS_PER_TOKEN = 3.5


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #
def ensure_session_dir(work_dir: str | os.PathLike) -> Path:
    """Create and return `<work_dir>/.mewcode/session/tool-results`."""
    path = Path(work_dir) / SESSION_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_tool_results(session_dir: str | os.PathLike) -> None:
    """Wipe and recreate the spill directory (called after a successful compact)."""
    path = Path(session_dir)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# State containers + transcript
# --------------------------------------------------------------------------- #
@dataclass
class CompactEvent:
    before_tokens: int


@dataclass
class ContentReplacementState:
    """Cross-turn log of which tool results were replaced.

    Invariant: ``replacements.keys() ⊆ seen_ids``.
    """

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class ContentReplacementRecord:
    tool_use_id: str
    replacement: str
    kind: str = "tool-result"


def create_replacement_state() -> ContentReplacementState:
    return ContentReplacementState()


def clone_replacement_state(src: ContentReplacementState) -> ContentReplacementState:
    """Independent shallow copy (values are immutable strings / hashable ids)."""
    return ContentReplacementState(set(src.seen_ids), dict(src.replacements))


def append_replacement_records(
    session_dir: str | os.PathLike, records: list[ContentReplacementRecord]
) -> None:
    if not records:
        return
    path = Path(session_dir) / REPLACEMENT_RECORDS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(
                json.dumps(
                    {
                        "kind": r.kind,
                        "tool_use_id": r.tool_use_id,
                        "replacement": r.replacement,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def load_replacement_records(
    session_dir: str | os.PathLike,
) -> list[ContentReplacementRecord]:
    path = Path(session_dir) / REPLACEMENT_RECORDS_FILENAME
    if not path.exists():
        return []
    out: list[ContentReplacementRecord] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(
                ContentReplacementRecord(
                    tool_use_id=d["tool_use_id"],
                    replacement=d["replacement"],
                    kind=d.get("kind", "tool-result"),
                )
            )
    return out


def reconstruct_replacement_state(
    messages: list[Message],
    records: list[ContentReplacementRecord],
    inherited_replacements: dict[str, str] | None = None,
) -> ContentReplacementState:
    seen_ids = {
        tr.tool_use_id for m in messages for tr in m.tool_results
    }
    replacements: dict[str, str] = {}
    for r in records:
        if r.kind == "tool-result" and r.tool_use_id in seen_ids:
            replacements[r.tool_use_id] = r.replacement
    if inherited_replacements:
        for tid, val in inherited_replacements.items():
            if tid in seen_ids and tid not in replacements:
                replacements[tid] = val
    return ContentReplacementState(seen_ids, replacements)


# --------------------------------------------------------------------------- #
# Layer 1: spill + preview
# --------------------------------------------------------------------------- #
def persist_tool_result(
    tool_use_id: str, content: str, session_dir: str | os.PathLike
) -> str:
    """Write full content to `<session_dir>/<tool_use_id>.txt` (idempotent)."""
    path = Path(session_dir) / f"{tool_use_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, content.encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
    except FileExistsError:
        pass  # already spilled this id — stable bytes, nothing to do
    return str(path)


def make_persisted_preview(content: str, file_path: str) -> str:
    """Byte-stable preview anchor. Do NOT change the format once shipped."""
    kb = max(1, len(content) // 1024)
    return (
        f"{PERSISTED_TAG}\n"
        f"输出太大（{kb}KB），完整内容已保存到：\n"
        f"{file_path}\n\n"
        f"预览（前 2KB）：\n"
        f"{content[:PREVIEW_CHARS]}\n"
        f"</persisted-output>"
    )


def _count_turns(messages: list[Message]) -> int:
    return sum(1 for m in messages if m.role == "assistant" and not m.tool_uses)


def _copy_message_with_results(
    msg: Message, new_tool_results: list[ToolResultBlock]
) -> Message:
    return Message(
        role=msg.role,
        content=msg.content,
        thinking_blocks=msg.thinking_blocks,
        tool_uses=msg.tool_uses,
        tool_results=new_tool_results,
    )


def _snip_stale_messages(history: list[Message]) -> list[Message]:
    """Pass 3: snip large, old, unmarked tool results beyond KEEP_RECENT_TURNS."""
    if _count_turns(history) <= KEEP_RECENT_TURNS:
        return history
    # Find the index where the last KEEP_RECENT_TURNS turns begin.
    turns_seen = 0
    boundary = 0
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.role == "assistant" and not m.tool_uses:
            turns_seen += 1
            if turns_seen >= KEEP_RECENT_TURNS:
                boundary = i
                break
    out: list[Message] = []
    for i, msg in enumerate(history):
        if i >= boundary or not msg.tool_results:
            out.append(msg)
            continue
        new_trs: list[ToolResultBlock] = []
        for tr in msg.tool_results:
            c = tr.content
            if (
                len(c) > OLD_RESULT_SNIP_CHARS
                and not c.startswith(PERSISTED_TAG)
                and not c.startswith(SNIPPED_TAG)
            ):
                snipped = (
                    f"{SNIPPED_TAG}\n"
                    f"(旧结果已裁剪，原始长度 {len(c)} 字符)\n"
                    f"{c[:200]}\n… (snipped)"
                )
                new_trs.append(ToolResultBlock(tr.tool_use_id, snipped, tr.is_error))
            else:
                new_trs.append(tr)
        out.append(_copy_message_with_results(msg, new_trs))
    return out


def apply_tool_result_budget(
    conversation: ConversationManager,
    session_dir: str | os.PathLike,
    state: ContentReplacementState,
) -> tuple[ConversationManager, list[ContentReplacementRecord]]:
    """Layer 1 (Design B): returns a NEW ConversationManager; input untouched."""
    new_records: list[ContentReplacementRecord] = []
    new_history: list[Message] = []

    for msg in conversation.history:
        if not msg.tool_results:
            new_history.append(msg)
            continue

        decisions: dict[str, str] = {}
        fresh: list[ToolResultBlock] = []

        # Stage 1: classify
        for tr in msg.tool_results:
            tid = tr.tool_use_id
            if tid in state.replacements:
                decisions[tid] = state.replacements[tid]  # byte-identical re-read
            elif tid in state.seen_ids:
                decisions[tid] = tr.content  # frozen: keep original
            elif tr.content.startswith(PERSISTED_TAG):
                state.seen_ids.add(tid)
                state.replacements[tid] = tr.content
                decisions[tid] = tr.content
                new_records.append(ContentReplacementRecord(tid, tr.content))
            else:
                fresh.append(tr)

        # Stage 2 (Pass 1): single oversize
        remaining: list[ToolResultBlock] = []
        for tr in fresh:
            if len(tr.content) > SINGLE_RESULT_CHAR_LIMIT:
                path = persist_tool_result(tr.tool_use_id, tr.content, session_dir)
                preview = make_persisted_preview(tr.content, path)
                state.seen_ids.add(tr.tool_use_id)
                state.replacements[tr.tool_use_id] = preview
                decisions[tr.tool_use_id] = preview
                new_records.append(
                    ContentReplacementRecord(tr.tool_use_id, preview)
                )
            else:
                remaining.append(tr)

        # Stage 3 (Pass 2): aggregate — pick largest fresh until under cap
        total = sum(len(v) for v in decisions.values()) + sum(
            len(tr.content) for tr in remaining
        )
        if total > AGGREGATE_CHAR_LIMIT:
            for tr in sorted(remaining, key=lambda t: len(t.content), reverse=True):
                if total <= AGGREGATE_CHAR_LIMIT:
                    break
                path = persist_tool_result(tr.tool_use_id, tr.content, session_dir)
                preview = make_persisted_preview(tr.content, path)
                state.seen_ids.add(tr.tool_use_id)
                state.replacements[tr.tool_use_id] = preview
                decisions[tr.tool_use_id] = preview
                new_records.append(
                    ContentReplacementRecord(tr.tool_use_id, preview)
                )
                total -= len(tr.content) - len(preview)

        # Stage 4: freeze any remaining undecided fresh as "not replaced"
        for tr in fresh:
            if tr.tool_use_id not in decisions:
                state.seen_ids.add(tr.tool_use_id)
                decisions[tr.tool_use_id] = tr.content

        new_trs = [
            ToolResultBlock(tr.tool_use_id, decisions[tr.tool_use_id], tr.is_error)
            for tr in msg.tool_results
        ]
        new_history.append(_copy_message_with_results(msg, new_trs))

    new_history = _snip_stale_messages(new_history)
    api_conv = ConversationManager(
        history=new_history,
        env_injected=conversation.env_injected,
        ltm_injected=conversation.ltm_injected,
        last_input_tokens=conversation.last_input_tokens,
    )
    return api_conv, new_records


# --------------------------------------------------------------------------- #
# Layer 2: summarize
# --------------------------------------------------------------------------- #
def compute_compact_threshold(context_window: int, manual: bool = False) -> int:
    margin = MANUAL_COMPACT_SAFETY_MARGIN if manual else AUTO_COMPACT_SAFETY_MARGIN
    # Floor at half the window so small windows still trigger sensibly (the
    # fixed reserve+margin would otherwise push the threshold near/below zero).
    return max(context_window - SUMMARY_OUTPUT_RESERVE - margin, context_window // 2)


def should_auto_compact(last_input_tokens: int, context_window: int) -> bool:
    return last_input_tokens >= compute_compact_threshold(context_window)


# --------------------------------------------------------------------------- #
# Token estimation (tiktoken, with a char-based fallback)
# --------------------------------------------------------------------------- #
_ENCODER: Any = None
_ENCODER_TRIED = False


def estimate_tokens(text: str) -> int:
    """Estimate token count via tiktoken (cl100k_base); fall back to len//4."""
    global _ENCODER, _ENCODER_TRIED
    if not text:
        return 0
    if not _ENCODER_TRIED:
        _ENCODER_TRIED = True
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 — tiktoken optional / offline
            _ENCODER = None
    if _ENCODER is not None:
        return len(_ENCODER.encode(text))
    return max(1, len(text) // 4)


def estimate_conversation_tokens(
    conversation: ConversationManager,
    system: str = "",
    tools: list[Mapping[str, Any]] | None = None,
) -> int:
    """Rough pre-request token estimate of the whole prompt.

    Lets compaction trigger *before* sending an oversized request instead of
    waiting for the previous turn's reported usage.
    """
    total = estimate_tokens(system) if system else 0
    for msg in conversation.history:
        if msg.content:
            total += estimate_tokens(msg.content)
        for tb in msg.thinking_blocks:
            total += estimate_tokens(tb.thinking)
        for tu in msg.tool_uses:
            total += estimate_tokens(str(tu.input))
        for tr in msg.tool_results:
            total += estimate_tokens(tr.content)
    if tools:
        for sc in tools:
            total += estimate_tokens(json.dumps(sc, ensure_ascii=False))
    return total


SUMMARY_PROMPT = """\
你是一个会话压缩器。绝对不要调用任何工具——这一轮只输出文本。

请把下面的完整对话压缩成一份结构化摘要，必须严格包含以下九节（缺一不可）：

1. 主要请求与意图：用户最初和后续的核心诉求，逐条列出。
2. 关键概念：涉及的技术概念、框架、约束。
3. 文件与代码段：读过/改过的文件路径与关键代码片段。
4. 错误与修复：遇到的报错和对应修复。
5. 解决过程：为达成目标做了哪些尝试与决策。
6. 用户原话：逐字摘录对理解意图最关键的用户原句。
7. 待办任务：尚未完成的事项。
8. 当前工作：被中断时正在做的事。
9. 下一步：紧接着应该做什么。

要求：先在 <analysis> 标签里写分析草稿（梳理上面九节各有哪些内容），
再在 <summary> 标签里写正式摘要。<analysis> 用完即弃，只有 <summary> 会被保留。

再次强调：本轮严禁调用任何工具，只输出 <analysis> 和 <summary> 文本。
"""


def extract_summary(llm_output: str) -> str:
    """Pull <summary>...</summary>; if missing, return the whole output."""
    start = llm_output.find("<summary>")
    end = llm_output.find("</summary>")
    if start != -1 and end != -1 and end > start:
        return llm_output[start + len("<summary>") : end].strip()
    return llm_output.strip()


COMPACT_BOUNDARY_MESSAGE = (
    "以上是对更早对话的有损压缩摘要。如果需要文件或代码的完整内容，"
    "请用 ReadFile 重新读取，不要根据摘要臆造不存在的代码。"
)


def build_compact_messages(summary: str, attachment: str = "") -> list[Message]:
    user_content = f"[摘要]\n{summary}"
    if attachment:
        user_content += f"\n\n---\n\n{attachment}"
    return [
        Message(role="user", content=user_content),
        Message(role="assistant", content=COMPACT_BOUNDARY_MESSAGE),
    ]


def _group_messages_by_turn(messages: list[Message]) -> list[list[Message]]:
    turns: list[list[Message]] = []
    cur: list[Message] = []
    for m in messages:
        cur.append(m)
        if m.role == "assistant" and not m.tool_uses:
            turns.append(cur)
            cur = []
    if cur:
        turns.append(cur)
    return turns


@dataclass
class CompactCircuitBreaker:
    max_failures: int = 3
    consecutive_failures: int = field(init=False, default=0)

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def is_open(self) -> bool:
        return self.consecutive_failures >= self.max_failures


def _is_prompt_too_long(exc: Exception) -> bool:
    s = str(exc).lower()
    return "prompt is too long" in s or "prompt_too_long" in s or "too long" in s


def _estimate_message_tokens(msg: Message) -> int:
    """Cheap token estimate for one message (content + tool blocks)."""
    total = estimate_tokens(msg.content) if msg.content else 0
    for tb in msg.thinking_blocks:
        total += estimate_tokens(tb.thinking)
    for tu in msg.tool_uses:
        total += estimate_tokens(str(tu.input))
    for tr in msg.tool_results:
        total += estimate_tokens(tr.content)
    return total


def _split_keep_recent(
    history: list[Message],
) -> tuple[list[Message], list[Message]]:
    """Split history into (old_to_summarize, recent_to_keep_verbatim).

    Walks messages from the newest, keeping them until the recent tail has at
    least MIN_KEEP_MESSAGES messages AND KEEP_RECENT_TOKENS tokens (never past
    KEEP_MAX_TOKENS). The split index is then snapped to a SAFE boundary so the
    old part never ends on a dangling assistant ``tool_use`` (whose
    ``tool_result`` would otherwise be orphaned into the recent part). Working
    at message granularity — not whole turns — lets compaction fire mid
    tool-execution, where every assistant message still carries a tool_use and
    there is no "final" assistant turn yet.
    """
    n = len(history)
    if n == 0:
        return [], []

    kept_tokens = 0
    kept_msgs = 0
    split = n  # recent = history[split:]
    for i in range(n - 1, -1, -1):
        t = _estimate_message_tokens(history[i])
        if kept_msgs >= MIN_KEEP_MESSAGES and kept_tokens + t > KEEP_MAX_TOKENS:
            break
        kept_tokens += t
        kept_msgs += 1
        split = i
        if kept_msgs >= MIN_KEEP_MESSAGES and kept_tokens >= KEEP_RECENT_TOKENS:
            break

    # Snap to a safe boundary: if old (history[:split]) would end on an
    # assistant message with tool_uses, pull that message (and its result,
    # already in recent) into recent by moving the split earlier.
    while (
        split > 0
        and history[split - 1].role == "assistant"
        and history[split - 1].tool_uses
    ):
        split -= 1

    return history[:split], history[split:]


async def _summarize_once(client: Any, history: list[Message]) -> str:
    from mewcode.tools.base import TextDelta

    summary_conv = ConversationManager(history=list(history))
    summary_conv.add_user_message(
        "请现在生成摘要。再次提醒：不要调用任何工具，只输出 <analysis> 与 <summary>。"
    )
    text = ""
    async for event in client.stream(summary_conv, system=SUMMARY_PROMPT, tools=[]):
        if isinstance(event, TextDelta):
            text += event.text
    return text


async def auto_compact(
    conversation: ConversationManager,
    client: Any,
    context_window: int,
    session_dir: str | os.PathLike,
    *,
    protocol: str = "anthropic",
    manual: bool = False,
    breaker: CompactCircuitBreaker | None = None,
    recovery: "RecoveryState | None" = None,
    tool_schemas: list[Mapping[str, Any]] | None = None,
    estimated_tokens: int | None = None,
) -> CompactEvent | str | None:
    """Layer 2: summarize the OLD part of history, keep the recent tail verbatim.

    The trigger uses the larger of the last reported usage and a pre-request
    ``estimated_tokens`` estimate, so a turn that is *about* to overflow
    compacts before the oversized request is ever sent. On success the new
    history is ``[summary, boundary, *recent_tail]`` — recent turns are kept as
    originals (controlled by KEEP_RECENT_TOKENS / MIN_KEEP_MESSAGES /
    KEEP_MAX_TOKENS) so the model keeps precise recent context.
    """
    threshold = compute_compact_threshold(context_window, manual)
    current = max(conversation.last_input_tokens, estimated_tokens or 0)
    if not manual and current < threshold:
        return None
    if manual and not conversation.history:
        return None
    if breaker is not None and breaker.is_open():
        return "上下文压缩已熔断（连续失败次数过多），已停止自动触发。"

    old_messages, recent_messages = _split_keep_recent(conversation.history)
    if not old_messages:
        # Everything fits in the recent tail — nothing old to summarize.
        return None

    before_tokens = current
    history = list(old_messages)
    last_error = ""
    for _attempt in range(3):
        try:
            text = await _summarize_once(client, history)
        except Exception as e:  # noqa: BLE001
            if _is_prompt_too_long(e) and len(history) > 1:
                turns = _group_messages_by_turn(history)
                drop = max(1, len(turns) // 5)
                history = [m for turn in turns[drop:] for m in turn]
                last_error = str(e)
                continue
            last_error = str(e)
            break
        summary = extract_summary(text)
        if not summary.strip():
            last_error = "empty summary"
            break
        attachment = (
            build_recovery_attachment(
                recovery,
                tool_schemas,
                skip_paths=_recent_read_paths(recent_messages),
            )
            if recovery is not None
            else ""
        )
        new_history = build_compact_messages(summary, attachment) + recent_messages
        conversation.replace_history(new_history)
        conversation.last_input_tokens = 0
        cleanup_tool_results(session_dir)
        if breaker is not None:
            breaker.record_success()
        return CompactEvent(before_tokens)

    if breaker is not None:
        breaker.record_failure()
    return f"上下文压缩失败：{last_error or '未知错误'}"


# --------------------------------------------------------------------------- #
# Post-compact recovery
# --------------------------------------------------------------------------- #
@dataclass
class FileReadRecord:
    path: str
    content: str
    timestamp: float


@dataclass
class SkillInvocationRecord:
    name: str
    body: str
    timestamp: float


class RecoveryState:
    """Thread-safe record of recently read files + activated skills."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, FileReadRecord] = {}
        self._skills: dict[str, SkillInvocationRecord] = {}

    def record_file_read(self, path: str, content: str) -> None:
        if not path:
            return
        with self._lock:
            self._files[path] = FileReadRecord(path, content, time.time())

    def record_skill_invocation(self, name: str, body: str) -> None:
        if not name:
            return
        with self._lock:
            self._skills[name] = SkillInvocationRecord(name, body, time.time())

    def snapshot_files(self, limit: int) -> list[FileReadRecord]:
        with self._lock:
            items = list(self._files.values())
        items.sort(key=lambda r: r.timestamp, reverse=True)
        return items[:limit]

    def snapshot_skills(self) -> list[SkillInvocationRecord]:
        with self._lock:
            items = list(self._skills.values())
        items.sort(key=lambda r: r.timestamp, reverse=True)
        return items


def _approx_tokens(s: str) -> int:
    return int(len(s) / _RECOVERY_CHARS_PER_TOKEN)
def _truncate_by_tokens(s: str, budget_tokens: int) -> str:
    max_chars = int(budget_tokens * _RECOVERY_CHARS_PER_TOKEN)
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n… (内容已截断)"


def _first_line(s: str) -> str:
    for line in s.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _schema_name_desc(schema: Mapping[str, Any]) -> tuple[str, str]:
    if "function" in schema and isinstance(schema["function"], Mapping):
        fn = schema["function"]
        return fn.get("name", ""), fn.get("description", "")
    return schema.get("name", ""), schema.get("description", "")


def _recent_read_paths(messages: list[Message]) -> set[str]:
    """Paths of files read in `messages` (their content is kept verbatim, so the
    recovery block must NOT re-embed them — that would double-count tokens)."""
    paths: set[str] = set()
    for m in messages:
        for tu in m.tool_uses:
            if tu.name == "ReadFile":
                p = tu.input.get("file_path") or tu.input.get("path")
                if p:
                    paths.add(str(p))
    return paths


def build_recovery_attachment(
    state: "RecoveryState | None",
    tool_schemas: list[Mapping[str, Any]] | None,
    skip_paths: set[str] | None = None,
) -> str:
    sections: list[str] = []
    skip_paths = skip_paths or set()

    files = state.snapshot_files(RECOVERY_FILE_LIMIT) if state else []
    files = [f for f in files if f.path not in skip_paths]
    if files:
        # Paths only — NOT content. Re-embedding file bytes would defeat the
        # summary (compaction wouldn't shrink a file-heavy conversation). The
        # model re-reads with ReadFile if it needs the current content.
        lines = ["## 最近读过的文件（内容未保留；如需具体内容请用 ReadFile 重新读取）"]
        for f in files:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.timestamp))
            lines.append(f"- {f.path}  (读于 {ts})")
        sections.append("\n".join(lines))

    skills = state.snapshot_skills() if state else []
    if skills:
        lines = ["## 已激活的技能"]
        used = 0
        for s in skills:
            chunk = _truncate_by_tokens(s.body, RECOVERY_TOKENS_PER_SKILL)
            used += _approx_tokens(chunk)
            if used > RECOVERY_SKILLS_BUDGET:
                break
            lines.append(f"### {s.name}")
            lines.append(chunk)
        if len(lines) > 1:
            sections.append("\n".join(lines))

    if tool_schemas:
        lines = ["## 可用工具"]
        for sc in tool_schemas:
            name, desc = _schema_name_desc(sc)
            if name:
                lines.append(f"- {name} — {_first_line(desc)}")
        if len(lines) > 1:
            sections.append("\n".join(lines))

    if not sections:
        return ""

    sections.append(
        "## 提示\n如果需要文件或代码的完整内容，请用 ReadFile 重新读取，"
        "不要根据本摘要臆造不存在的代码。"
    )
    return "\n\n".join(sections)
