"""Adapt a discovered MCP tool to MewCode's `Tool` ABC (ch07).

`MCPToolWrapper` makes a remote tool look exactly like a built-in tool to the
Agent Loop and the permission system: it has a name, a Pydantic params model
(generated from the server's JSON Schema), and an async ``execute`` that round
trips through the server's session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, create_model

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mcp import types

    from mewcode.mcp.client import MCPClient


_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _json_type_to_python(json_type: object) -> type:
    """Map a JSON Schema ``type`` to a Python type (unknown -> Any)."""
    if isinstance(json_type, list):  # e.g. ["string", "null"]
        for t in json_type:
            if t != "null":
                json_type = t
                break
    return _JSON_TO_PY.get(json_type, Any)  # type: ignore[arg-type]


def _build_params_model(tool_name: str, schema: dict) -> type[BaseModel]:
    """Build a Pydantic model from an MCP tool's ``inputSchema``.

    Required props become mandatory fields (``...``); the rest are optional
    with a ``None`` default.
    """
    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])
    fields: dict[str, tuple] = {}
    for fname, fdef in props.items():
        py = _json_type_to_python((fdef or {}).get("type"))
        if fname in required:
            fields[fname] = (py, ...)
        else:
            fields[fname] = (Optional[py], None)
    model_name = "".join(c for c in tool_name.title() if c.isalnum()) + "Params"
    return create_model(model_name, **fields)  # type: ignore[call-overload]


def _extract_text(result: "types.CallToolResult") -> str:
    """Flatten an MCP tool result's content blocks into a single string.

    Handles TextContent / ImageContent / EmbeddedResource; falls back to
    ``(no output)`` when nothing renderable is present.
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", ""))
        elif btype == "image":
            parts.append(f"[image: {getattr(block, 'mimeType', 'unknown')}]")
        elif btype == "resource":
            res = getattr(block, "resource", None)
            text = getattr(res, "text", None)
            if text:
                parts.append(text)
            else:
                parts.append(f"[resource: {getattr(res, 'uri', '')}]")
    joined = "\n".join(p for p in parts if p)
    return joined or "(no output)"


class MCPToolWrapper(Tool):
    """Wraps one MCP tool so the registry can treat it like any other tool."""

    # Remote tools are side-effecting by default and stay out of the initial
    # schema list until ToolSearch surfaces them (keeps the prompt small).
    category = "command"
    should_defer = True

    def __init__(
        self, server_name: str, tool_def: "types.Tool", client: "MCPClient"
    ) -> None:
        self.server_name = server_name
        self._tool_def = tool_def
        self._client = client
        # Instance-level shadows of the ABC's ClassVars.
        self.name = f"mcp_{server_name}_{tool_def.name}"
        self.description = tool_def.description or ""
        self._input_schema = tool_def.inputSchema or {
            "type": "object",
            "properties": {},
        }
        self.params_model = _build_params_model(self.name, self._input_schema)

    def get_schema(self) -> dict[str, Any]:
        # Return the server's raw inputSchema verbatim — converting through
        # Pydantic can lose schema semantics the server relies on.
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._input_schema,
        }

    async def execute(self, params: BaseModel) -> ToolResult:
        arguments = params.model_dump(exclude_none=True)
        try:
            if not self._client.is_alive:
                await self._client.connect()  # lazy reconnect
            result = await self._client.call_tool(self._tool_def.name, arguments)
        except Exception as e:  # noqa: BLE001 — surface as tool error, don't crash
            return ToolResult(output=f"MCP tool error: {e}", is_error=True)
        return ToolResult(
            output=_extract_text(result),
            is_error=bool(getattr(result, "isError", False)),
        )
