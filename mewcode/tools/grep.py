"""Grep tool — search file contents by regex."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import SKIP_DIRS, Tool, ToolResult


class Params(BaseModel):
    pattern: str = Field(description="Regular expression to search for.")
    path: str = Field(default=".", description="Base directory to search.")
    include: str = Field(
        default="", description="Optional basename glob filter, e.g. *.py"
    )


class Grep(Tool):
    name = "Grep"
    description = (
        "Search file contents with a regex; returns `<file>:<line>:<text>` hits."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        try:
            regex = re.compile(params.pattern)
        except re.error as e:
            return ToolResult(f"Error: invalid regex: {e}", is_error=True)

        base = Path(params.path)
        if not base.exists():
            return ToolResult(f"Error: path not found: {params.path}", is_error=True)

        glob_pat = f"**/{params.include}" if params.include else "**/*"
        hits: list[str] = []
        for p in base.glob(glob_pat):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = p.relative_to(base)
            for line_num, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{line_num}:{line}")

        if not hits:
            return ToolResult("No matches found.")
        return ToolResult("\n".join(hits))
