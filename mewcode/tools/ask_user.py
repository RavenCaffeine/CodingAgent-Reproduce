"""AskUserQuestion — a deferred tool that asks the user structured questions.

The tool hands a structured question to the TUI via an asyncio.Future and
blocks (up to 5 minutes) until the TUI resolves it. The TUI reads the pending
event from `_pending_event` and calls `future.set_result(...)`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

ASK_TIMEOUT = 300  # 5 minutes


class QuestionItem(BaseModel):
    type: str = Field(default="single", description="single | multi | text")
    name: str = Field(description="Identifier for this question's answer.")
    message: str = Field(description="Question text shown to the user.")
    options: list[str] = Field(default_factory=list, description="Choices.")


class AskUserParams(BaseModel):
    questions: list[QuestionItem] = Field(description="Questions to ask.")


@dataclass
class AskUserEvent:
    """Pending question handed to the TUI; resolved via `future`."""

    questions: list[QuestionItem]
    future: "asyncio.Future[dict[str, str]]"


class AskUserTool(Tool):
    name = "AskUserQuestion"
    description = (
        "Ask the user one or more structured multiple-choice questions and "
        "wait for their answers."
    )
    params_model = AskUserParams
    category = "read"
    should_defer = True
    is_system_tool = True

    def __init__(self) -> None:
        # The TUI polls this to render a pending question.
        self._pending_event: AskUserEvent | None = None

    @property
    def pending_event(self) -> AskUserEvent | None:
        return self._pending_event

    async def execute(self, params: AskUserParams) -> ToolResult:  # type: ignore[override]
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, str]] = loop.create_future()
        self._pending_event = AskUserEvent(params.questions, future)
        try:
            answers = await asyncio.wait_for(future, timeout=ASK_TIMEOUT)
        except asyncio.TimeoutError:
            return ToolResult(
                "User did not respond within 5 minutes", is_error=True
            )
        finally:
            self._pending_event = None

        lines = [f"{q.name}: {answers.get(q.name, '')}" for q in params.questions]
        return ToolResult("\n".join(lines))
