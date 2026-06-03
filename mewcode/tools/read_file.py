"""ReadFile tool — read a text file with 1-based line numbers."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.cache import FileCache
from mewcode.tools.base import Tool, ToolResult


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to read.")
    offset: int = Field(default=0, description="0-based line offset to start at.")
    limit: int = Field(default=2000, description="Max number of lines to read.")


class ReadFile(Tool):
    name = "ReadFile"
    description = (
        "Read a text file and return its contents with 1-based line numbers, "
        "formatted as `<line_no>\\t<content>`."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    def __init__(self, file_cache: FileCache | None = None) -> None:
        self._cache = file_cache

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(f"Error: file not found: {params.file_path}", is_error=True)
        if not path.is_file():
            return ToolResult(f"Error: not a file: {params.file_path}", is_error=True)

        key = str(path.resolve())
        text = self._cache.get(key) if self._cache else None
        if text is None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return ToolResult(f"Error reading file: {e}", is_error=True)
            if self._cache:
                self._cache.put(key, text)

        lines = text.splitlines()
        window = lines[params.offset : params.offset + params.limit]
        numbered = "\n".join(
            f"{i + params.offset + 1}\t{line}" for i, line in enumerate(window)
        )
        return ToolResult(numbered)
