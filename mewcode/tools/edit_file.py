"""EditFile tool — unique-match string replacement."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.cache import FileCache
from mewcode.tools.base import Tool, ToolResult


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to edit.")
    old_string: str = Field(description="Exact text to replace (must be unique).")
    new_string: str = Field(description="Replacement text.")


class EditFile(Tool):
    name = "EditFile"
    description = (
        "Replace a unique occurrence of old_string with new_string in a file. "
        "Read the file first so old_string matches exactly. "
        "Fails if old_string is missing or appears more than once."
    )
    params_model = Params
    category = "write"

    def __init__(self, file_cache: FileCache | None = None) -> None:
        self._cache = file_cache

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(f"Error: file not found: {params.file_path}", is_error=True)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult(f"Error reading file: {e}", is_error=True)

        count = content.count(params.old_string)
        if count == 0:
            return ToolResult("Error: old_string not found in file", is_error=True)
        if count > 1:
            return ToolResult(
                f"Error: old_string found {count} times, must be unique",
                is_error=True,
            )

        updated = content.replace(params.old_string, params.new_string, 1)
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as e:
            return ToolResult(f"Error writing file: {e}", is_error=True)
        if self._cache:
            self._cache.invalidate(str(path.resolve()))
        return ToolResult(f"Successfully edited {params.file_path}")
