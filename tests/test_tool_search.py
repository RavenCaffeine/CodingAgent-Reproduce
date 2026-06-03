"""ch03 tests — ToolSearch deferred disclosure + AskUserQuestion."""

from __future__ import annotations

import asyncio

import pytest

from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool, QuestionItem
from mewcode.tools.impl.tool_search import ToolSearchTool


def _registry_with_deferred():
    r = create_default_registry()
    r.register(ToolSearchTool(r, protocol="anthropic"))
    r.register(AskUserTool())
    return r


def test_deferred_hidden_until_discovered() -> None:
    r = _registry_with_deferred()
    names = [s["name"] for s in r.get_all_schemas()]
    assert "AskUserQuestion" not in names      # deferred, hidden
    assert "ToolSearch" in names               # not deferred, visible
    assert r.get_deferred_tool_names() == ["AskUserQuestion"]


@pytest.mark.asyncio
async def test_tool_search_select_by_name() -> None:
    r = _registry_with_deferred()
    ts = r.get("ToolSearch")
    res = await ts.execute(ts.params_model(query="select:AskUserQuestion"))
    assert "AskUserQuestion" in res.output
    # now discovered -> shows up in schema list
    assert "AskUserQuestion" in [s["name"] for s in r.get_all_schemas()]
    assert r.is_discovered("AskUserQuestion")


@pytest.mark.asyncio
async def test_tool_search_by_keyword() -> None:
    r = _registry_with_deferred()
    ts = r.get("ToolSearch")
    res = await ts.execute(ts.params_model(query="question"))
    assert "AskUserQuestion" in res.output


@pytest.mark.asyncio
async def test_tool_search_no_match_lists_available() -> None:
    r = _registry_with_deferred()
    ts = r.get("ToolSearch")
    res = await ts.execute(ts.params_model(query="select:Nonexistent"))
    assert "No matching deferred tools" in res.output
    assert "AskUserQuestion" in res.output  # available list


def test_tool_search_schema_strips_title() -> None:
    r = _registry_with_deferred()
    schema = r.get("ToolSearch").get_schema()
    assert "title" not in schema["input_schema"]


@pytest.mark.asyncio
async def test_ask_user_resolves_via_future() -> None:
    tool = AskUserTool()
    params = tool.params_model(
        questions=[QuestionItem(name="color", message="Pick", options=["r", "b"])]
    )
    task = asyncio.ensure_future(tool.execute(params))
    # wait for the tool to register its pending event
    for _ in range(50):
        if tool.pending_event is not None:
            break
        await asyncio.sleep(0.01)
    assert tool.pending_event is not None
    tool.pending_event.future.set_result({"color": "blue"})
    res = await task
    assert res.output == "color: blue"
    assert res.is_error is False


@pytest.mark.asyncio
async def test_ask_user_timeout(monkeypatch) -> None:
    import mewcode.tools.ask_user as au

    monkeypatch.setattr(au, "ASK_TIMEOUT", 0.05)
    tool = AskUserTool()
    params = tool.params_model(
        questions=[QuestionItem(name="q", message="m", options=["a"])]
    )
    res = await tool.execute(params)
    assert res.is_error is True
    assert "within 5 minutes" in res.output
