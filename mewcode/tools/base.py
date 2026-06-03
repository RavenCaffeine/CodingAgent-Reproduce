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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Tool primitives (ch03)
# --------------------------------------------------------------------------- #

# Directories every file-walking tool (Glob / Grep) skips wholesale.
SKIP_DIRS: set[str] = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
}

# Per-tool result ceiling. ch04/ch08 truncate or spill to disk past this so a
# single tool can't blow up the next request's context.
MAX_OUTPUT_CHARS = 10000

# Permission class + parallel-batch hint. read-only tools can run concurrently;
# write / command tools run serially.
ToolCategory = Literal["read", "write", "command"]


@dataclass
class ToolResult:
    """Uniform tool return value, fed back into the conversation as-is."""

    output: str
    is_error: bool = False


class Tool(ABC):
    """Abstract base every tool (built-in / MCP / Skill / Team) implements.

    The five-part contract: name / description / params_model / category /
    execute. `params_model` is a Pydantic model, so `get_schema()` derives the
    JSON Schema for free (no hand-written schema per provider).
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    params_model: ClassVar[type[BaseModel]]
    category: ClassVar[ToolCategory] = "read"

    # read-only tools that are safe to run in the same parallel batch
    is_concurrency_safe: ClassVar[bool] = False
    # system/internal tools (e.g. AskUserQuestion) — not user-facing file ops
    is_system_tool: ClassVar[bool] = False
    # deferred tools stay out of the initial schema list until ToolSearch
    # discovers them
    should_defer: ClassVar[bool] = False

    @property
    def is_read_only(self) -> bool:
        return self.category == "read"

    def get_schema(self) -> dict[str, Any]:
        """Anthropic-shaped schema derived from the Pydantic params model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.params_model.model_json_schema(),
        }

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult:
        """Run the tool. Must be cancellable (respond to CancelledError)."""
        raise NotImplementedError


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
