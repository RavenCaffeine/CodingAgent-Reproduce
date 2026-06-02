"""ch02 integration tests — LLM client, events, and conversation model.

These exercise the ch02-core deliverables without the full Agent Loop (which
lands in a later chapter). A MockLLMClient stands in for a real provider so the
streaming contract and serialization round-trip are testable offline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from mewcode.client import (
    AuthenticationError,
    LLMClient,
    LLMError,
    NetworkError,
    RateLimitError,
    _supports_adaptive_thinking,
    create_client,
)
from mewcode.config import ProviderConfig
from mewcode.conversation import (
    ConversationManager,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from mewcode.tools.agent_tool import _create_client_for_model
from mewcode.tools.base import (
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)


# --------------------------------------------------------------------------- #
# Mock client
# --------------------------------------------------------------------------- #


class MockLLMClient(LLMClient):
    """Replays scripted event batches, one batch per stream() call."""

    def __init__(self, responses: list[list[Any]]) -> None:
        super().__init__()
        self._responses = responses
        self._call = 0

    async def stream(  # type: ignore[override]
        self,
        conversation: ConversationManager,
        system: str,
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        batch = self._responses[self._call]
        self._call += 1
        for event in batch:
            yield event


async def _drain(client: LLMClient, conv: ConversationManager) -> list[Any]:
    return [e async for e in client.stream(conv, system="", tools=[])]


# --------------------------------------------------------------------------- #
# Streaming contract
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_single_step_tool_call() -> None:
    client = MockLLMClient(
        [
            [
                TextDelta("Let me check."),
                ToolCallStart("t1", "read_file"),
                ToolCallDelta("t1", '{"path":'),
                ToolCallDelta("t1", '"a.py"}'),
                ToolCallComplete("t1", "read_file", {"path": "a.py"}),
                StreamEnd("tool_use", input_tokens=10, output_tokens=5),
            ]
        ]
    )
    events = await _drain(client, ConversationManager())
    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    assert len(completes) == 1
    assert completes[0].arguments == {"path": "a.py"}
    assert isinstance(events[-1], StreamEnd)
    assert events[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_thinking_complete_carries_signature() -> None:
    client = MockLLMClient(
        [
            [
                ThinkingDelta("hmm"),
                ThinkingComplete("hmm let me think", signature="sig-xyz"),
                StreamEnd("end_turn"),
            ]
        ]
    )
    events = await _drain(client, ConversationManager())
    tc = [e for e in events if isinstance(e, ThinkingComplete)][0]
    assert tc.signature == "sig-xyz"


@pytest.mark.asyncio
async def test_token_usage_accumulates() -> None:
    client = MockLLMClient(
        [
            [StreamEnd("tool_use", input_tokens=100, output_tokens=20)],
            [StreamEnd("end_turn", input_tokens=130, output_tokens=15)],
        ]
    )
    conv = ConversationManager()
    total_in = total_out = 0
    for _ in range(2):
        async for event in client.stream(conv, system="", tools=[]):
            if isinstance(event, StreamEnd):
                total_in += event.input_tokens
                total_out += event.output_tokens
    assert total_in == 230
    assert total_out == 35


# --------------------------------------------------------------------------- #
# Serialization round-trip (message splicing)
# --------------------------------------------------------------------------- #


def test_message_splicing() -> None:
    """serialize('anthropic') yields 5 messages with no field loss."""
    conv = ConversationManager()
    conv.inject_environment("CWD=/repo")
    conv.add_user_message("fix the bug")
    conv.add_assistant_message(
        "I'll read two files.",
        tool_uses=[
            ToolUseBlock("t1", "read_file", {"path": "a.py"}),
            ToolUseBlock("t2", "read_file", {"path": "b.py"}),
        ],
        thinking_blocks=[ThinkingBlock("planning", signature="sig-1")],
    )
    conv.add_tool_results_message(
        [
            ToolResultBlock("t1", "contents of a"),
            ToolResultBlock("t2", "boom", is_error=True),
        ]
    )
    conv.add_assistant_message("Fixed it.")

    msgs = conv.serialize("anthropic")
    assert len(msgs) == 5

    # env + user + assistant(blocks) + user(tool_results) + assistant(text)
    assert msgs[0]["role"] == "user"  # env reminder
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"

    blocks = msgs[2]["content"]
    kinds = [b["type"] for b in blocks]
    assert kinds == ["thinking", "text", "tool_use", "tool_use"]
    assert blocks[0]["signature"] == "sig-1"  # signature survives
    assert blocks[2]["input"] == {"path": "a.py"}  # tool input survives

    results = msgs[3]["content"]
    assert results[0]["type"] == "tool_result"
    assert results[1]["is_error"] is True  # is_error survives

    assert msgs[4]["content"] == "Fixed it."  # plain text path


def test_anthropic_reminder_merges_into_prior_user() -> None:
    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.add_system_reminder("be concise")
    msgs = conv.serialize("anthropic")
    # reminder merged into the preceding user message -> single user turn
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    texts = [b["text"] for b in msgs[0]["content"]]
    assert texts[0] == "hello"
    assert "<system-reminder>" in texts[1]


def test_openai_serialization_shapes() -> None:
    conv = ConversationManager()
    conv.add_user_message("hi")
    conv.add_assistant_message(
        "calling tool",
        tool_uses=[ToolUseBlock("c1", "ls", {"dir": "."})],
    )
    conv.add_tool_results_message([ToolResultBlock("c1", "file1\nfile2")])
    items = conv.serialize("openai")
    types = [it.get("type") or it.get("role") for it in items]
    assert "function_call" in types
    assert "function_call_output" in types
    fc = [it for it in items if it.get("type") == "function_call"][0]
    assert fc["call_id"] == "c1"
    assert fc["name"] == "ls"
    fco = [it for it in items if it.get("type") == "function_call_output"][0]
    assert fco["output"] == "file1\nfile2"


def test_inject_is_idempotent() -> None:
    conv = ConversationManager()
    conv.inject_environment("ctx")
    conv.inject_environment("ctx")  # second call is a no-op
    assert sum(1 for m in conv.history if m.content.startswith("<system-reminder>")) == 1


def test_get_messages_is_shallow_copy() -> None:
    conv = ConversationManager()
    conv.add_user_message("x")
    copy = conv.get_messages()
    copy.append(object())  # type: ignore[arg-type]
    assert len(conv.history) == 1


# --------------------------------------------------------------------------- #
# Errors / factory / thinking / model map
# --------------------------------------------------------------------------- #


def test_error_hierarchy() -> None:
    for exc in (AuthenticationError, RateLimitError, NetworkError):
        assert issubclass(exc, LLMError)
    rl = RateLimitError("slow down", retry_after=12.5)
    assert rl.retry_after == 12.5


def test_create_client_unknown_protocol() -> None:
    cfg = ProviderConfig(name="x", protocol="grpc", model="m", api_key="k")
    with pytest.raises(ValueError, match="Unknown protocol: grpc"):
        create_client(cfg)


def test_create_client_missing_key_raises_auth() -> None:
    cfg = ProviderConfig(name="a", protocol="anthropic", model="claude-x")
    with pytest.raises(AuthenticationError):
        create_client(cfg)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-4-6", True),
        ("claude-sonnet-4-6", True),
        ("claude-sonnet-4-8", True),
        ("claude-sonnet-4-5", False),
        ("claude-opus-4-1", False),
        ("claude-haiku-4-5-20251001", False),
        ("gpt-5", False),
    ],
)
def test_supports_adaptive_thinking(model: str, expected: bool) -> None:
    assert _supports_adaptive_thinking(model) is expected


def test_model_short_name_mapping() -> None:
    parent = ProviderConfig(
        name="main", protocol="anthropic", model="claude-opus-4-6", api_key="k"
    )
    child = _create_client_for_model("sonnet", parent)
    # AnthropicClient stores the resolved config
    assert child._config.model == "claude-sonnet-4-6"  # type: ignore[attr-defined]
    # unknown alias passes through as a literal model id
    child2 = _create_client_for_model("claude-custom-9", parent)
    assert child2._config.model == "claude-custom-9"  # type: ignore[attr-defined]


def test_max_output_tokens_budget() -> None:
    plain = ProviderConfig(name="p", protocol="anthropic", model="m")
    assert plain.get_max_output_tokens() == 8192
    thinking = ProviderConfig(
        name="t", protocol="anthropic", model="m", thinking=True
    )
    assert thinking.get_max_output_tokens() == 64000


def test_set_max_output_tokens_clamps_to_ceiling() -> None:
    client = MockLLMClient([[StreamEnd("end_turn")]])
    client.set_max_output_tokens(10_000_000)
    from mewcode.config import MAX_TOKENS_CEILING

    assert client._max_output_tokens == MAX_TOKENS_CEILING


# --------------------------------------------------------------------------- #
# DeepSeek (Chat Completions protocol)
# --------------------------------------------------------------------------- #


def test_deepseek_chat_serialization() -> None:
    conv = ConversationManager()
    conv.add_user_message("hi")
    conv.add_assistant_message(
        "calling tool",
        tool_uses=[ToolUseBlock("c1", "ls", {"dir": "."})],
    )
    conv.add_tool_results_message([ToolResultBlock("c1", "file1")])
    conv.add_user_message("thanks")
    msgs = conv.serialize("deepseek")
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "user"]
    assistant = msgs[1]
    assert assistant["tool_calls"][0]["function"]["name"] == "ls"
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert msgs[2]["tool_call_id"] == "c1"


def test_create_client_routes_deepseek() -> None:
    from mewcode.client import DeepSeekClient

    cfg = ProviderConfig(
        name="ds",
        protocol="deepseek",
        model="deepseek-v4-pro",
        api_key="sk-ds",
        base_url="https://api.deepseek.com",
    )
    client = create_client(cfg)
    assert isinstance(client, DeepSeekClient)


def test_deepseek_missing_key_raises_auth() -> None:
    cfg = ProviderConfig(
        name="ds", protocol="deepseek", model="deepseek-v4-flash"
    )
    with pytest.raises(AuthenticationError):
        create_client(cfg)
