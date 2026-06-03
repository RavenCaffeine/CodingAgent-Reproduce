"""WriteFile tool — create or overwrite a file, making parent dirs."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.cache import FileCache
from mewcode.tools.base import Tool, ToolResult


class Params(BaseModel):
    file_path: str = Field(description="Path to write.")
    content: str = Field(description="Full file content to write.")


class WriteFile(Tool):
    name = "WriteFile"
    description = "Write content to a file, creating parent directories as needed."
    params_model = Params
    category = "write"

    def __init__(self, file_cache: FileCache | None = None) -> None:
        self._cache = file_cache

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        path = Path(params.file_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.content, encoding="utf-8")
        except OSError as e:
            return ToolResult(f"Error writing file: {e}", is_error=True)
        if self._cache:
            self._cache.invalidate(str(path.resolve()))
        return ToolResult(f"Successfully wrote to {params.file_path}")
