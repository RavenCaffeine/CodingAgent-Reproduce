"""LLM client layer.

Gives the whole system one interface — `LLMClient` — for talking to a provider.
Two built-in implementations (`AnthropicClient`, `OpenAIClient`) normalize SSE
streams into the `StreamEvent` union and map every SDK exception into one of
four `LLMError` subclasses, so callers only ever face `except LLMError`.

SDK packages (`anthropic`, `openai`) are imported lazily inside the concrete
clients so this module imports cleanly in environments (e.g. tests) where the
SDKs are not installed.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from mewcode.config import MAX_TOKENS_CEILING, ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)

# --------------------------------------------------------------------------- #
# Error hierarchy (T3)
# --------------------------------------------------------------------------- #


class LLMError(Exception):
    """Base class for every error the client layer raises."""


class AuthenticationError(LLMError):
    """Invalid or missing API key."""


class RateLimitError(LLMError):
    """Provider rate limit hit. `retry_after` is seconds, if known."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(LLMError):
    """Connection-level failure (DNS, TLS, timeout, reset)."""


# --------------------------------------------------------------------------- #
# Abstract base + factory (T1)
# --------------------------------------------------------------------------- #


class LLMClient(ABC):
    """Uniform async streaming interface to a provider."""

    def __init__(self) -> None:
        self._max_output_tokens: int = 8192

    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str,
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents for one assistant turn.

        Implementations are native async generators; cancellation is
        cooperative via asyncio (the caller cancels the task).
        """
        raise NotImplementedError

    def set_max_output_tokens(self, tokens: int) -> None:
        """Set the per-turn output budget (clamped to the ceiling)."""
        self._max_output_tokens = min(tokens, MAX_TOKENS_CEILING)


def _supports_adaptive_thinking(model: str) -> bool:
    """True for claude-opus-4- / claude-sonnet-4- with minor version >= 6."""
    for prefix in ("claude-opus-4-", "claude-sonnet-4-"):
        if model.startswith(prefix):
            rest = model[len(prefix):]
            return bool(rest) and rest[0].isdigit() and int(rest[0]) >= 6
    return False


# --------------------------------------------------------------------------- #
# Anthropic (T4)
# --------------------------------------------------------------------------- #

# ch08: prompt-cache breakpoints. Marking the system prompt, the last tool, and
# the tail of the last user message with an ephemeral cache_control lets
# Anthropic reuse the stable prefix (which Layer 1's byte-stable replacements
# keep intact) instead of re-billing it every turn.
_EPHEMERAL = {"type": "ephemeral"}


def _mark_last_user_tail_for_cache(messages: list[dict[str, Any]]) -> None:
    """Add a cache breakpoint to the last user message's final block (in place)."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            # up-convert string content into a single text block
            content = [{"type": "text", "text": content}]
            msg["content"] = content
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = _EPHEMERAL
        return


def _mark_last_tool_for_cache(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a shallow copy of tools with the last one cache-marked.

    Copies so the caller's shared tool table is not polluted across turns.
    """
    if not tools:
        return tools
    out = list(tools)
    out[-1] = {**out[-1], "cache_control": _EPHEMERAL}
    return out


class AnthropicClient(LLMClient):
    """Anthropic Messages API streaming client."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__()
        self._config = config
        self._max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError("Missing Anthropic API key")
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, base_url=config.base_url)

    def _thinking_kwarg(self) -> dict[str, Any]:
        if _supports_adaptive_thinking(self._config.model):
            return {"type": "enabled", "budget_tokens": 0}
        return {
            "type": "enabled",
            "budget_tokens": max(self._max_output_tokens - 1, 1024),
        }

    async def stream(  # type: ignore[override]
        self,
        conversation: ConversationManager,
        system: str,
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        import anthropic

        messages = conversation.serialize("anthropic")
        # ch08: cache breakpoint on the tail of the last user message.
        _mark_last_user_tail_for_cache(messages)
        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._max_output_tokens,
            "messages": messages,
        }
        if system:
            # ch08: cache the (stable) system prompt prefix.
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": _EPHEMERAL}
            ]
        if tools:
            # ch08: cache breakpoint after the tool definitions.
            kwargs["tools"] = _mark_last_tool_for_cache(tools)
        if self._config.thinking:
            kwargs["thinking"] = self._thinking_kwarg()

        # per-block accumulation state
        cur_kind: str | None = None
        cur_tool_id = ""
        cur_tool_name = ""
        cur_json = ""
        cur_thinking = ""
        cur_signature = ""

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    etype = event.type
                    if etype == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            cur_kind = "thinking"
                            cur_thinking = ""
                            cur_signature = ""
                        elif block.type == "tool_use":
                            cur_kind = "tool"
                            cur_tool_id = block.id
                            cur_tool_name = block.name
                            cur_json = ""
                            yield ToolCallStart(cur_tool_id, cur_tool_name)
                        elif block.type == "text":
                            cur_kind = "text"
                    elif etype == "content_block_delta":
                        delta = event.delta
                        dtype = delta.type
                        if dtype == "text_delta":
                            yield TextDelta(delta.text)
                        elif dtype == "thinking_delta":
                            cur_thinking += delta.thinking
                            yield ThinkingDelta(delta.thinking)
                        elif dtype == "signature_delta":
                            cur_signature += delta.signature
                        elif dtype == "input_json_delta":
                            cur_json += delta.partial_json
                            yield ToolCallDelta(cur_tool_id, delta.partial_json)
                    elif etype == "content_block_stop":
                        if cur_kind == "thinking":
                            yield ThinkingComplete(cur_thinking, cur_signature)
                        elif cur_kind == "tool":
                            args = json.loads(cur_json) if cur_json else {}
                            yield ToolCallComplete(
                                cur_tool_id, cur_tool_name, args
                            )
                        cur_kind = None

                final = await stream.get_final_message()
                usage = final.usage
                yield StreamEnd(
                    stop_reason=final.stop_reason or "end_turn",
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                )
        except anthropic.AuthenticationError as e:
            raise AuthenticationError(str(e)) from e
        except anthropic.RateLimitError as e:
            raise RateLimitError(str(e), retry_after=_retry_after(e)) from e
        except anthropic.APIConnectionError as e:
            raise NetworkError(str(e)) from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e}") from e


def _retry_after(exc: Any) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value is not None else None
    except (AttributeError, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# OpenAI (T5)
# --------------------------------------------------------------------------- #


class OpenAIClient(LLMClient):
    """OpenAI Responses API streaming client (not Chat Completions)."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__()
        self._config = config
        self._max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError("Missing OpenAI API key")
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=config.base_url)

    async def stream(  # type: ignore[override]
        self,
        conversation: ConversationManager,
        system: str,
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        import openai

        input_items = conversation.serialize("openai")
        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "input": input_items,
            "max_output_tokens": self._max_output_tokens,
            "stream": True,
        }
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = tools

        tool_name = ""
        call_id = ""
        json_accum = ""
        started = False

        try:
            response_stream = await self._client.responses.create(**kwargs)
            async for event in response_stream:
                etype = event.type
                if etype == "response.output_text.delta":
                    yield TextDelta(event.delta)
                elif etype == "response.output_item.added":
                    item = event.item
                    if getattr(item, "type", None) == "function_call":
                        tool_name = item.name
                        call_id = item.call_id
                        json_accum = ""
                        started = True
                        yield ToolCallStart(call_id, tool_name)
                elif etype == "response.function_call_arguments.delta":
                    if not started:
                        # delta arrived before output_item.added
                        call_id = getattr(event, "item_id", call_id)
                        tool_name = getattr(event, "name", tool_name)
                        started = True
                        yield ToolCallStart(call_id, tool_name)
                    json_accum += event.delta
                    yield ToolCallDelta(call_id, event.delta)
                elif etype == "response.function_call_arguments.done":
                    args = json.loads(json_accum) if json_accum else {}
                    yield ToolCallComplete(call_id, tool_name, args)
                    started = False
                elif etype == "response.completed":
                    usage = event.response.usage
                    yield StreamEnd(
                        stop_reason="end_turn",
                        input_tokens=getattr(usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    )
        except openai.AuthenticationError as e:
            raise AuthenticationError(str(e)) from e
        except openai.RateLimitError as e:
            raise RateLimitError(str(e), retry_after=_retry_after(e)) from e
        except openai.APIConnectionError as e:
            raise NetworkError(str(e)) from e
        except openai.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e}") from e


# --------------------------------------------------------------------------- #
# DeepSeek
# --------------------------------------------------------------------------- #


class DeepSeekClient(LLMClient):
    """DeepSeek streaming client via the OpenAI **Chat Completions** API.

    DeepSeek (V4-Pro / V4-Flash) is OpenAI-compatible at the Chat Completions
    layer (not the Responses API), so we reuse the OpenAI SDK pointed at
    `https://api.deepseek.com`. Thinking-mode models stream their chain of
    thought as `delta.reasoning_content`, which we surface as ThinkingDelta /
    ThinkingComplete.
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__()
        self._config = config
        self._max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError("Missing DeepSeek API key")
        from openai import AsyncOpenAI

        base_url = config.base_url or "https://api.deepseek.com"
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def stream(  # type: ignore[override]
        self,
        conversation: ConversationManager,
        system: str,
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        import openai

        messages = conversation.serialize("deepseek")
        if system:
            messages = [{"role": "system", "content": system}, *messages]

        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": self._max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        reasoning_buf = ""
        thinking_open = False
        tool_calls: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        in_tokens = out_tokens = 0

        try:
            response_stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in response_stream:
                if getattr(chunk, "usage", None):
                    in_tokens = chunk.usage.prompt_tokens or 0
                    out_tokens = chunk.usage.completion_tokens or 0
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    thinking_open = True
                    reasoning_buf += reasoning
                    yield ThinkingDelta(reasoning)

                content = getattr(delta, "content", None)
                if content:
                    if thinking_open:
                        yield ThinkingComplete(reasoning_buf)
                        thinking_open = False
                        reasoning_buf = ""
                    yield TextDelta(content)

                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = tc.index
                    slot = tool_calls.setdefault(
                        idx, {"id": "", "name": "", "args": "", "started": False}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn and fn.name:
                        slot["name"] = fn.name
                    if not slot["started"] and slot["id"] and slot["name"]:
                        slot["started"] = True
                        yield ToolCallStart(slot["id"], slot["name"])
                    if fn and fn.arguments:
                        slot["args"] += fn.arguments
                        yield ToolCallDelta(slot["id"], fn.arguments)

                if choice.finish_reason:
                    stop_reason = choice.finish_reason
                    for slot in tool_calls.values():
                        args = json.loads(slot["args"]) if slot["args"] else {}
                        yield ToolCallComplete(slot["id"], slot["name"], args)

            if thinking_open:
                yield ThinkingComplete(reasoning_buf)
            yield StreamEnd(stop_reason, in_tokens, out_tokens)
        except openai.AuthenticationError as e:
            raise AuthenticationError(str(e)) from e
        except openai.RateLimitError as e:
            raise RateLimitError(str(e), retry_after=_retry_after(e)) from e
        except openai.APIConnectionError as e:
            raise NetworkError(str(e)) from e
        except openai.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e}") from e


# --------------------------------------------------------------------------- #
# Factory (T1 / F2)
# --------------------------------------------------------------------------- #


def create_client(config: ProviderConfig) -> LLMClient:
    """Route to the right client by `config.protocol`."""
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    if config.protocol == "openai":
        return OpenAIClient(config)
    if config.protocol == "deepseek":
        return DeepSeekClient(config)
    raise ValueError(f"Unknown protocol: {config.protocol}")
