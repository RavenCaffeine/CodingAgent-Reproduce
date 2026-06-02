"""Streaming event model shared across the client and Agent Loop.

The five signal families a provider stream emits are normalized here into a
small set of dataclasses so the rest of the system can dispatch with
`isinstance` and never touch provider-specific SSE payloads.

Signal families (spec.md F3):
  1. text            -> TextDelta
  2. thinking        -> ThinkingDelta, ThinkingComplete (carries signature)
  3. tool call       -> ToolCallStart, ToolCallDelta, ToolCallComplete
  4. stream lifecycle -> StreamEnd (stop reason + token usage)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Streaming events
# --------------------------------------------------------------------------- #


@dataclass
class TextDelta:
    """A chunk of assistant-visible text."""

    text: str


@dataclass
class ThinkingDelta:
    """A chunk of Extended Thinking text (not shown as final answer)."""

    text: str


@dataclass
class ThinkingComplete:
    """A thinking block finished. `signature` must be replayed next turn."""

    thinking: str
    signature: str = ""


@dataclass
class ToolCallStart:
    """A tool call began. `tool_id` correlates the later delta/complete."""

    tool_id: str
    tool_name: str


@dataclass
class ToolCallDelta:
    """Incremental partial JSON for a tool call's arguments."""

    tool_id: str
    partial_json: str


@dataclass
class ToolCallComplete:
    """A tool call's arguments are fully parsed and ready to execute."""

    tool_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamEnd:
    """The stream finished. Carries stop reason and token usage."""

    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0


# Union of every event a `LLMClient.stream` may yield. Dispatch with isinstance.
StreamEvent = (
    TextDelta
    | ThinkingDelta
    | ThinkingComplete
    | ToolCallStart
    | ToolCallDelta
    | ToolCallComplete
    | StreamEnd
)
