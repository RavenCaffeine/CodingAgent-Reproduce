"""Tests for ch08 context management: Layer 1, Layer 2, session helpers."""

from __future__ import annotations

import pytest

from mewcode.context.manager import (
    AGGREGATE_CHAR_LIMIT,
    PERSISTED_TAG,
    SINGLE_RESULT_CHAR_LIMIT,
    CompactCircuitBreaker,
    CompactEvent,
    apply_tool_result_budget,
    auto_compact,
    build_compact_messages,
    compute_compact_threshold,
    create_replacement_state,
    ensure_session_dir,
    estimate_conversation_tokens,
    estimate_tokens,
    extract_summary,
    make_persisted_preview,
    persist_tool_result,
    should_auto_compact,
)
from mewcode.conversation import ConversationManager, Message, ToolResultBlock
from mewcode.tools.base import TextDelta


def _conv_with_results(results):
    conv = ConversationManager()
    conv.history.append(Message(role="user", tool_results=list(results)))
    return conv


class TestSessionDir:
    def test_ensure_creates_dir(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        assert d.exists() and d.is_dir()
        assert d.name == "tool-results"


class TestPersistToolResult:
    def test_writes_full_content(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        path = persist_tool_result("tid1", "X" * 9000, d)
        assert open(path, encoding="utf-8").read() == "X" * 9000

    def test_idempotent_existing(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        p1 = persist_tool_result("tid", "first", d)
        p2 = persist_tool_result("tid", "second-ignored", d)  # O_EXCL -> no-op
        assert p1 == p2
        assert open(p1, encoding="utf-8").read() == "first"


class TestMakePersistedPreview:
    def test_format(self):
        out = make_persisted_preview("Y" * 4000, "/tmp/x.txt")
        assert out.startswith(PERSISTED_TAG)
        assert "/tmp/x.txt" in out
        assert out.endswith("</persisted-output>")
        assert "Y" * 100 in out  # preview present


class TestApplyToolResultBudget:
    def test_does_not_mutate_conv(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        big = "Z" * (SINGLE_RESULT_CHAR_LIMIT + 100)
        conv = _conv_with_results([ToolResultBlock("t1", big)])
        before = conv.history[0].tool_results[0].content
        apply_tool_result_budget(conv, d, create_replacement_state())
        assert conv.history[0].tool_results[0].content == before  # untouched

    def test_single_oversize_persisted(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        big = "Z" * (SINGLE_RESULT_CHAR_LIMIT + 100)
        conv = _conv_with_results([ToolResultBlock("t1", big)])
        api_conv, records = apply_tool_result_budget(
            conv, d, create_replacement_state()
        )
        content = api_conv.history[0].tool_results[0].content
        assert content.startswith(PERSISTED_TAG)
        assert (d / "t1.txt").exists()
        assert any(r.tool_use_id == "t1" for r in records)

    def test_small_result_untouched(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        conv = _conv_with_results([ToolResultBlock("t1", "small")])
        api_conv, records = apply_tool_result_budget(
            conv, d, create_replacement_state()
        )
        assert api_conv.history[0].tool_results[0].content == "small"
        assert records == []

    def test_aggregate_picks_largest(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        # three mid results, sum over aggregate cap, none over single cap
        n = AGGREGATE_CHAR_LIMIT // 3 + 500
        results = [
            ToolResultBlock("a", "A" * (n - 100)),
            ToolResultBlock("b", "B" * (n + 200)),  # largest -> persisted first
            ToolResultBlock("c", "C" * (n - 100)),
        ]
        conv = _conv_with_results(results)
        api_conv, _ = apply_tool_result_budget(conv, d, create_replacement_state())
        by_id = {r.tool_use_id: r.content for r in api_conv.history[0].tool_results}
        assert by_id["b"].startswith(PERSISTED_TAG)  # biggest spilled


class TestComputeCompactThreshold:
    def test_auto(self):
        assert compute_compact_threshold(200_000) == 167_000
        assert compute_compact_threshold(128_000) == 95_000

    def test_manual(self):
        assert compute_compact_threshold(200_000, manual=True) == 177_000

    def test_small_window_floors_at_half(self):
        # reserve+margin would push this negative; floor at window//2.
        assert compute_compact_threshold(50_000) == 25_000
        assert compute_compact_threshold(40_000) == 20_000

    def test_should_auto_compact_boundary(self):
        assert should_auto_compact(167_000, 200_000) is True
        assert should_auto_compact(166_999, 200_000) is False


class TestEstimateTokens:
    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_monotonic(self):
        assert estimate_tokens("a" * 10) <= estimate_tokens("a" * 1000)

    def test_conversation_sums_results(self):
        conv = ConversationManager()
        conv.history.append(
            Message(role="user", tool_results=[ToolResultBlock("t", "X" * 4000)])
        )
        assert estimate_conversation_tokens(conv) > 0


class TestExtractSummary:
    def test_extracts_tag_pair(self):
        assert extract_summary("<analysis>x</analysis><summary>  S  </summary>") == "S"

    def test_missing_tag_returns_whole(self):
        assert extract_summary("no tags here") == "no tags here"


class TestBuildCompactMessages:
    def test_two_messages(self):
        msgs = build_compact_messages("the summary")
        assert len(msgs) == 2
        assert msgs[0].role == "user" and msgs[0].content.startswith("[摘要]")
        assert "the summary" in msgs[0].content
        assert msgs[1].role == "assistant"

    def test_attachment_appended(self):
        msgs = build_compact_messages("S", attachment="REC")
        assert "---" in msgs[0].content and "REC" in msgs[0].content


class TestCompactCircuitBreaker:
    def test_trips_after_max(self):
        b = CompactCircuitBreaker(max_failures=3)
        assert not b.is_open()
        b.record_failure()
        b.record_failure()
        assert not b.is_open()
        b.record_failure()
        assert b.is_open()

    def test_success_resets(self):
        b = CompactCircuitBreaker(max_failures=2)
        b.record_failure()
        b.record_failure()
        assert b.is_open()
        b.record_success()
        assert not b.is_open()


class _FakeSummaryClient:
    """Streams a fixed summary; or raises to simulate failure."""

    def __init__(self, text="<summary>OK SUMMARY</summary>", fail=False):
        self._text = text
        self._fail = fail
        self.tools_seen = None

    async def stream(self, conversation, system, tools):
        self.tools_seen = tools
        if self._fail:
            raise RuntimeError("boom")
        yield TextDelta(self._text)


def _long_conv(tokens):
    """Many turns with enough content that some are 'old' (summarizable) and a
    recent tail can be kept verbatim under the keep-recent budget."""
    conv = ConversationManager()
    body = "alpha beta gamma delta epsilon " * 200  # ~hundreds of tokens
    for i in range(40):
        conv.add_user_message(f"用户问题 {i}")
        conv.add_assistant_message(f"{body} 回答 {i}")
    conv.last_input_tokens = tokens
    return conv


class TestAutoCompact:
    @pytest.mark.asyncio
    async def test_skips_below_threshold(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        conv = _long_conv(1000)
        out = await auto_compact(conv, _FakeSummaryClient(), 200_000, d)
        assert out is None

    @pytest.mark.asyncio
    async def test_compacts_above_threshold_keeps_recent_tail(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        conv = _long_conv(180_000)
        client = _FakeSummaryClient()
        out = await auto_compact(conv, client, 200_000, d)
        assert isinstance(out, CompactEvent)
        assert out.before_tokens == 180_000
        assert client.tools_seen == []  # tools disabled during summary
        # new history = [summary, boundary, *recent_tail]
        assert conv.history[0].content.startswith("[摘要]")
        assert "OK SUMMARY" in conv.history[0].content
        assert conv.history[1].role == "assistant"  # boundary message
        # the newest original turn is kept verbatim in the tail
        assert any("用户问题 39" in m.content for m in conv.history)
        # but an early turn was folded into the summary (not kept verbatim)
        assert not any("用户问题 0" == m.content for m in conv.history)
        assert len(conv.history) > 2

    @pytest.mark.asyncio
    async def test_failure_keeps_history_and_trips_breaker(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        conv = _long_conv(180_000)
        before = list(conv.history)
        breaker = CompactCircuitBreaker(max_failures=1)
        out = await auto_compact(
            conv, _FakeSummaryClient(fail=True), 200_000, d, breaker=breaker
        )
        assert isinstance(out, str)  # error string
        assert conv.history == before  # unchanged
        assert breaker.is_open()

    @pytest.mark.asyncio
    async def test_breaker_open_returns_error(self, tmp_path):
        d = ensure_session_dir(tmp_path)
        conv = _long_conv(180_000)
        breaker = CompactCircuitBreaker(max_failures=1)
        breaker.record_failure()  # already open
        out = await auto_compact(
            conv, _FakeSummaryClient(), 200_000, d, breaker=breaker
        )
        assert isinstance(out, str)

    @pytest.mark.asyncio
    async def test_estimated_tokens_triggers_proactively(self, tmp_path):
        # last_input_tokens is low (0), but the pre-request estimate is over the
        # threshold -> compaction must fire before sending the oversized request.
        d = ensure_session_dir(tmp_path)
        conv = _long_conv(0)
        out = await auto_compact(
            conv, _FakeSummaryClient(), 200_000, d, estimated_tokens=180_000
        )
        assert isinstance(out, CompactEvent)
        assert out.before_tokens == 180_000


class TestSplitKeepRecent:
    def test_short_conv_all_recent(self):
        from mewcode.context.manager import _split_keep_recent

        conv = ConversationManager()
        conv.add_user_message("hi")
        conv.add_assistant_message("hello")
        old, recent = _split_keep_recent(conv.history)
        assert old == []  # nothing old to summarize
        assert len(recent) == 2

    def test_long_conv_splits_old_and_recent(self):
        from mewcode.context.manager import (
            KEEP_MAX_TOKENS,
            _estimate_message_tokens,
            _split_keep_recent,
        )

        conv = ConversationManager()
        body = "alpha beta gamma delta " * 200
        for i in range(40):
            conv.add_user_message(f"q{i}")
            conv.add_assistant_message(f"{body} a{i}")
        old, recent = _split_keep_recent(conv.history)
        assert old and recent  # both non-empty
        # recent tail stays within the hard cap
        assert sum(_estimate_message_tokens(m) for m in recent) <= KEEP_MAX_TOKENS
        # the newest message is in the recent tail, the oldest is in 'old'
        assert recent[-1].content.endswith("a39")
        assert old[0].content == "q0"


class TestSplitMidToolExecution:
    def test_splits_during_unfinished_tool_loop(self):
        """ch08 fix: compaction must be able to fire mid tool-execution, where
        every assistant message still carries a tool_use (no final turn yet)."""
        from mewcode.conversation import ToolUseBlock
        from mewcode.context.manager import _split_keep_recent

        conv = ConversationManager()
        conv.add_user_message("梳理代码结构")
        for i in range(6):
            conv.add_assistant_message(
                "", tool_uses=[ToolUseBlock(f"t{i}", "Bash", {"command": f"c{i}"})]
            )
            conv.history.append(
                Message(role="user", tool_results=[ToolResultBlock(f"t{i}", "R" * 4000)])
            )
        old, recent = _split_keep_recent(conv.history)
        assert old, "must produce an old part to summarize mid-tool-execution"
        assert recent
        # safe boundary: old never ends on a dangling assistant tool_use
        assert not (old[-1].role == "assistant" and old[-1].tool_uses)
        # recent never starts with an orphaned tool_result
        assert not (
            recent[0].role == "user"
            and recent[0].tool_results
            and not recent[0].content
        )
