"""ch04 Agent Loop tests — multi-round, termination, batching, plan, cancel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from mewcode.agent import (
    Agent,
    ErrorEvent,
    LoopComplete,
    ToolResultEvent,
    ToolUseEvent,
    partition_tool_calls,
)
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager, ToolUseBlock
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolCallStart


class ScriptedClient(LLMClient):
    """Replays scripted StreamEvent batches, one per stream() call."""

    def __init__(self, batches: list[list[Any]]) -> None:
        super().__init__()
        self._batches = batches
        self.calls = 0
        self.max_tokens_set: list[int] = []

    def set_max_output_tokens(self, tokens: int) -> None:
        super().set_max_output_tokens(tokens)
        self.max_tokens_set.append(tokens)

    async def stream(self, conversation, system, tools) -> AsyncIterator[Any]:  # type: ignore[override]
        batch = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        for e in batch:
            yield e


def _tool_call(tid: str, name: str, args: dict) -> list[Any]:
    return [
        ToolCallStart(tid, name),
        ToolCallComplete(tid, name, args),
    ]


async def _collect(agent: Agent, conv: ConversationManager) -> list[Any]:
    return [e async for e in agent.run(conv)]


# --- multi-round / termination -------------------------------------------- #


@pytest.mark.asyncio
async def test_multi_step_autonomous(tmp_path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\nworld\n")
    client = ScriptedClient([
        # round 1: read the file
        [TextDelta("Reading."), *_tool_call("t1", "ReadFile", {"file_path": str(f)}),
         StreamEnd("tool_use", 10, 5)],
        # round 2: no tools -> done
        [TextDelta("Done."), StreamEnd("end_turn", 8, 3)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("read x.txt")
    events = await _collect(agent, conv)

    assert any(isinstance(e, ToolUseEvent) for e in events)
    assert any(isinstance(e, ToolResultEvent) and not e.is_error for e in events)
    assert isinstance(events[-1], LoopComplete)
    assert events[-1].iterations == 2
    assert client.calls == 2


@pytest.mark.asyncio
async def test_stop_end_turn_no_tools(tmp_path) -> None:
    client = ScriptedClient([[TextDelta("Hi!"), StreamEnd("end_turn", 1, 1)]])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("hello")
    events = await _collect(agent, conv)
    assert isinstance(events[-1], LoopComplete)
    assert events[-1].iterations == 1
    assert client.calls == 1


@pytest.mark.asyncio
async def test_stop_max_iterations(tmp_path) -> None:
    # always asks for a tool -> never terminates on its own
    client = ScriptedClient([
        [*_tool_call("t", "ReadFile", {"file_path": str(tmp_path / "none")}),
         StreamEnd("tool_use", 1, 1)],
    ])
    agent = Agent(
        client, create_default_registry(), "anthropic",
        work_dir=str(tmp_path), max_iterations=3,
    )
    conv = ConversationManager()
    conv.add_user_message("loop forever")
    events = await _collect(agent, conv)
    assert isinstance(events[-1], ErrorEvent)
    assert "max iterations" in events[-1].message


@pytest.mark.asyncio
async def test_token_usage_accumulates(tmp_path) -> None:
    client = ScriptedClient([
        [*_tool_call("t1", "ReadFile", {"file_path": str(tmp_path / "n")}),
         StreamEnd("tool_use", 100, 20)],
        [TextDelta("ok"), StreamEnd("end_turn", 30, 10)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("go")
    await _collect(agent, conv)
    assert agent.total_input_tokens == 130
    assert agent.total_output_tokens == 30


# --- max_tokens escalation ------------------------------------------------- #


@pytest.mark.asyncio
async def test_max_tokens_escalation(tmp_path) -> None:
    from mewcode.agent import RetryEvent

    client = ScriptedClient([
        [TextDelta("partial"), StreamEnd("max_tokens", 5, 5)],
        [TextDelta("rest, done"), StreamEnd("end_turn", 5, 5)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("write a lot")
    events = await _collect(agent, conv)
    assert any(isinstance(e, RetryEvent) for e in events)
    assert client.max_tokens_set  # set_max_output_tokens was called
    assert isinstance(events[-1], LoopComplete)


# --- batching -------------------------------------------------------------- #


def test_partition_tool_calls() -> None:
    reg = create_default_registry()
    calls = [
        ToolUseBlock("1", "ReadFile", {}),   # read (safe)
        ToolUseBlock("2", "Grep", {}),       # read (safe) -> same batch
        ToolUseBlock("3", "WriteFile", {}),  # write -> own batch
        ToolUseBlock("4", "Glob", {}),       # read (safe) -> own batch
        ToolUseBlock("5", "Bash", {}),       # command -> own batch
    ]
    batches = partition_tool_calls(calls, reg)
    assert [len(b.calls) for b in batches] == [2, 1, 1, 1]
    assert batches[0].concurrent is True
    assert batches[1].concurrent is False  # WriteFile
    assert batches[3].concurrent is False  # Bash


@pytest.mark.asyncio
async def test_concurrent_batch_execution(tmp_path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("aaa")
    b = tmp_path / "b.txt"
    b.write_text("bbb")
    client = ScriptedClient([
        [*_tool_call("t1", "ReadFile", {"file_path": str(a)}),
         *_tool_call("t2", "ReadFile", {"file_path": str(b)}),
         StreamEnd("tool_use", 1, 1)],
        [TextDelta("both read"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("read both")
    events = await _collect(agent, conv)
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(results) == 2
    assert all(not r.is_error for r in results)


# --- plan mode ------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plan_mode_blocks_write(tmp_path) -> None:
    target = tmp_path / "out.txt"
    client = ScriptedClient([
        [*_tool_call("t1", "WriteFile", {"file_path": str(target), "content": "x"}),
         StreamEnd("tool_use", 1, 1)],
        [TextDelta("plan ready"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(
        client, create_default_registry(), "anthropic",
        work_dir=str(tmp_path), plan_mode=True,
    )
    conv = ConversationManager()
    conv.add_user_message("change the file")
    events = await _collect(agent, conv)
    res = [e for e in events if isinstance(e, ToolResultEvent)][0]
    assert res.is_error is True
    assert "PLAN mode" in res.output
    assert not target.exists()  # no side effect


@pytest.mark.asyncio
async def test_plan_mode_allows_read(tmp_path) -> None:
    f = tmp_path / "r.txt"
    f.write_text("data")
    client = ScriptedClient([
        [*_tool_call("t1", "ReadFile", {"file_path": str(f)}),
         StreamEnd("tool_use", 1, 1)],
        [TextDelta("ok"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(
        client, create_default_registry(), "anthropic",
        work_dir=str(tmp_path), plan_mode=True,
    )
    conv = ConversationManager()
    conv.add_user_message("read it")
    events = await _collect(agent, conv)
    res = [e for e in events if isinstance(e, ToolResultEvent)][0]
    assert res.is_error is False


# --- cancellation ---------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancel_propagates(tmp_path) -> None:
    class SlowClient(LLMClient):
        async def stream(self, conversation, system, tools):  # type: ignore[override]
            yield TextDelta("start")
            await asyncio.sleep(5)
            yield StreamEnd("end_turn", 1, 1)

    agent = Agent(SlowClient(), create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("hang")

    async def drive():
        async for _ in agent.run(conv):
            pass

    task = asyncio.ensure_future(drive())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # history stays consistent: the user message is still there
    contents = [m.content for m in conv.get_messages()]
    assert "hang" in contents
