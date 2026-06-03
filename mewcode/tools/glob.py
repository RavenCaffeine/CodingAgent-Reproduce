"""Glob tool — list files matching a glob pattern."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import SKIP_DIRS, Tool, ToolResult


class Params(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. **/*.py")
    path: str = Field(default=".", description="Base directory to search from.")


class Glob(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern, returned in sorted order."
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        base = Path(params.path)
        if not base.exists():
            return ToolResult(f"Error: path not found: {params.path}", is_error=True)

        matches: list[str] = []
        for p in base.glob(params.pattern):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            matches.append(str(p.relative_to(base)))

        if not matches:
            return ToolResult("No files matched the pattern.")
        return ToolResult("\n".join(sorted(matches)))
