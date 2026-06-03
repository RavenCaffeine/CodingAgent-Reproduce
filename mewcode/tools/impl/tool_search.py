"""ToolSearch — progressive disclosure of deferred tools.

Deferred tools (MCP wrappers, Team ops, AskUserQuestion, …) stay out of the
initial schema list. The model calls ToolSearch to pull them in, either by
exact name (`select:Name1,Name2`) or by keyword. Matches are marked discovered
so they appear in subsequent schema exports.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.registry import ToolRegistry


class ToolSearchParams(BaseModel):
    query: str = Field(
        description=(
            "Either `select:Name1,Name2` to fetch tools by exact name, or "
            "keywords to search deferred tool names/descriptions."
        )
    )
    max_results: int = Field(default=5, description="Max tools to return.")


class ToolSearchTool(Tool):
    name = "ToolSearch"
    description = (
        "Fetch schemas for deferred tools so they become callable. Use "
        "`select:Name1,Name2` for exact names, or keywords to search."
    )
    params_model = ToolSearchParams
    category = "read"
    should_defer = False  # ToolSearch itself is never deferred

    def __init__(self, registry: ToolRegistry, protocol: str = "anthropic") -> None:
        self._registry = registry
        self._protocol = protocol

    def get_schema(self) -> dict[str, Any]:
        schema = super().get_schema()
        # Drop the auto-generated "title" noise from the JSON Schema.
        schema["input_schema"].pop("title", None)
        return schema

    async def execute(self, params: ToolSearchParams) -> ToolResult:  # type: ignore[override]
        query = params.query.strip()
        if query.startswith("select:"):
            names = [n for n in query[len("select:"):].split(",") if n.strip()]
            schemas = self._registry.find_deferred_by_names(names, self._protocol)
        else:
            schemas = self._registry.search_deferred(
                query, params.max_results, self._protocol
            )

        if not schemas:
            available = ", ".join(self._registry.get_deferred_tool_names()) or "(none)"
            return ToolResult(
                f'No matching deferred tools for "{query}". Available: {available}'
            )

        for s in schemas:
            self._registry.mark_discovered(s["name"])

        body = json.dumps(schemas, ensure_ascii=False, indent=2)
        return ToolResult(f"Found {len(schemas)} tool(s):\n{body}")
