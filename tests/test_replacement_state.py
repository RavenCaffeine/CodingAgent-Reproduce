"""Tests for the ContentReplacementState decision log (ch08 Layer 1)."""

from __future__ import annotations

from mewcode.context.manager import (
    AGGREGATE_CHAR_LIMIT,
    PERSISTED_TAG,
    SINGLE_RESULT_CHAR_LIMIT,
    ContentReplacementRecord,
    apply_tool_result_budget,
    append_replacement_records,
    clone_replacement_state,
    create_replacement_state,
    ensure_session_dir,
    load_replacement_records,
    reconstruct_replacement_state,
)
from mewcode.conversation import ConversationManager, Message, ToolResultBlock


def _conv(results):
    conv = ConversationManager()
    conv.history.append(Message(role="user", tool_results=list(results)))
    return conv


def test_create_returns_empty():
    s = create_replacement_state()
    assert s.seen_ids == set() and s.replacements == {}


def test_clone_independent():
    s = create_replacement_state()
    s.seen_ids.add("x")
    s.replacements["x"] = "v"
    c = clone_replacement_state(s)
    c.seen_ids.add("y")
    c.replacements["y"] = "w"
    assert "y" not in s.seen_ids and "y" not in s.replacements
    assert "x" in c.seen_ids  # original data copied


def test_apply_does_not_mutate_conv(tmp_path):
    d = ensure_session_dir(tmp_path)
    big = "Z" * (SINGLE_RESULT_CHAR_LIMIT + 50)
    conv = _conv([ToolResultBlock("t1", big)])
    before = conv.history[0].tool_results[0].content
    apply_tool_result_budget(conv, d, create_replacement_state())
    assert conv.history[0].tool_results[0].content == before


def test_first_call_freezes_unreplaced(tmp_path):
    d = ensure_session_dir(tmp_path)
    state = create_replacement_state()
    conv = _conv([ToolResultBlock("t1", "small")])
    apply_tool_result_budget(conv, d, state)
    assert "t1" in state.seen_ids
    assert "t1" not in state.replacements  # frozen as "not replaced"


def test_replacement_byte_identical(tmp_path):
    d = ensure_session_dir(tmp_path)
    state = create_replacement_state()
    big = "Z" * (SINGLE_RESULT_CHAR_LIMIT + 50)
    conv = _conv([ToolResultBlock("t1", big)])
    api1, _ = apply_tool_result_budget(conv, d, state)
    api2, _ = apply_tool_result_budget(conv, d, state)
    c1 = api1.history[0].tool_results[0].content
    c2 = api2.history[0].tool_results[0].content
    assert c1 == c2  # byte-identical re-read from state.replacements
    assert c1.startswith(PERSISTED_TAG)


def test_frozen_never_replaced(tmp_path):
    d = ensure_session_dir(tmp_path)
    state = create_replacement_state()
    # First turn: a single small result, frozen.
    conv1 = _conv([ToolResultBlock("t1", "S" * 100)])
    apply_tool_result_budget(conv1, d, state)
    # Later turn: same id reappears alongside content that pushes aggregate over.
    filler = "F" * (AGGREGATE_CHAR_LIMIT)
    conv2 = _conv(
        [ToolResultBlock("t1", "S" * 100), ToolResultBlock("t2", filler)]
    )
    api, _ = apply_tool_result_budget(conv2, d, state)
    by_id = {r.tool_use_id: r.content for r in api.history[0].tool_results}
    assert by_id["t1"] == "S" * 100  # frozen id never replaced
    assert by_id["t2"].startswith(PERSISTED_TAG)  # fresh big one spilled


def test_aggregate_only_picks_fresh(tmp_path):
    d = ensure_session_dir(tmp_path)
    state = create_replacement_state()
    n = AGGREGATE_CHAR_LIMIT // 2 + 100
    conv = _conv([ToolResultBlock("a", "A" * n), ToolResultBlock("b", "B" * n)])
    api, _ = apply_tool_result_budget(conv, d, state)
    spilled = [
        r for r in api.history[0].tool_results if r.content.startswith(PERSISTED_TAG)
    ]
    assert len(spilled) >= 1


def test_reconstruct_from_records():
    msgs = [Message(role="user", tool_results=[ToolResultBlock("t1", "x")])]
    records = [ContentReplacementRecord("t1", "PREVIEW")]
    state = reconstruct_replacement_state(msgs, records)
    assert state.seen_ids == {"t1"}
    assert state.replacements == {"t1": "PREVIEW"}


def test_reconstruct_with_inherited_parent():
    msgs = [Message(role="user", tool_results=[ToolResultBlock("t2", "x")])]
    state = reconstruct_replacement_state(
        msgs, records=[], inherited_replacements={"t2": "FROM_PARENT", "zz": "no"}
    )
    assert state.replacements == {"t2": "FROM_PARENT"}  # zz not in seen_ids


def test_append_and_load_records_roundtrip(tmp_path):
    d = ensure_session_dir(tmp_path)
    recs = [
        ContentReplacementRecord("t1", "P1"),
        ContentReplacementRecord("t2", "P2"),
    ]
    append_replacement_records(d, recs)
    append_replacement_records(d, [])  # empty no-op
    loaded = load_replacement_records(d)
    assert [(r.tool_use_id, r.replacement) for r in loaded] == [
        ("t1", "P1"),
        ("t2", "P2"),
    ]
    assert all(r.kind == "tool-result" for r in loaded)
