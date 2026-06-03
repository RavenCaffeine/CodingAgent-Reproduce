"""Tool registry and the default-registry factory.

The registry is the single place the Agent Loop maps a tool name to an executor
and exports the per-protocol schema list. It also implements *deferred* tools:
tools that stay out of the initial schema list until ToolSearch discovers them
(progressive tool disclosure).

Registry is not concurrency-safe — write only during assembly, read at runtime
(N2 in spec.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewcode.tools.base import Tool

if TYPE_CHECKING:
    from mewcode.cache import FileCache


class ToolRegistry:
    """Holds tool instances; looks them up; exports schemas per protocol."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()

    # --- basic ----------------------------------------------------------- #

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    # --- enable / disable ------------------------------------------------ #

    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def disable(self, name: str) -> None:
        self._disabled.add(name)

    def enable_all(self) -> None:
        self._disabled.clear()

    # --- deferred discovery ---------------------------------------------- #

    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered

    def get_deferred_tool_names(self) -> list[str]:
        """Deferred tools not yet discovered and not disabled."""
        return [
            t.name
            for t in self._tools.values()
            if t.should_defer
            and not self.is_discovered(t.name)
            and self.is_enabled(t.name)
        ]

    def search_deferred(
        self, query: str, max_results: int = 5, protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        """Keyword-rank deferred tools by name/description match."""
        terms = [w for w in query.lower().split() if w]
        scored: list[tuple[int, Tool]] = []
        for t in self._tools.values():
            if not t.should_defer or not self.is_enabled(t.name):
                continue
            name_l = t.name.lower()
            desc_l = t.description.lower()
            score = 0
            q = query.lower().strip()
            if q and q in name_l:
                score += 10
            if q and q in desc_l:
                score += 5
            for term in terms:
                if term in name_l:
                    score += 3
                elif term in desc_l:
                    score += 1
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._schema_for(t, protocol) for _, t in scored[:max_results]]

    def find_deferred_by_names(
        self, names: list[str], protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        """Exact-select deferred tools by name (for `select:A,B`)."""
        out: list[dict[str, Any]] = []
        for name in names:
            t = self._tools.get(name.strip())
            if t and t.should_defer and self.is_enabled(t.name):
                out.append(self._schema_for(t, protocol))
        return out

    # --- schema export --------------------------------------------------- #

    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        """Schemas for all enabled, non-deferred (or discovered) tools."""
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            if not self.is_enabled(t.name):
                continue
            if t.should_defer and not self.is_discovered(t.name):
                continue
            out.append(self._schema_for(t, protocol))
        return out

    @staticmethod
    def _schema_for(tool: Tool, protocol: str) -> dict[str, Any]:
        schema = tool.get_schema()  # Anthropic shape
        if protocol == "openai":
            # OpenAI Responses API: flat function entry.
            return {
                "type": "function",
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["input_schema"],
            }
        if protocol == "deepseek":
            # OpenAI Chat Completions: function nested under "function".
            return {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["input_schema"],
                },
            }
        return schema


def create_default_registry(file_cache: "FileCache | None" = None) -> ToolRegistry:
    """Register the six core tools and return a ready registry."""
    from mewcode.tools.bash import Bash
    from mewcode.tools.edit_file import EditFile
    from mewcode.tools.glob import Glob
    from mewcode.tools.grep import Grep
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import WriteFile

    registry = ToolRegistry()
    registry.register(ReadFile(file_cache))
    registry.register(WriteFile(file_cache))
    registry.register(EditFile(file_cache))
    registry.register(Bash())
    registry.register(Glob())
    registry.register(Grep())
    return registry
