"""MCP (Model Context Protocol) client package (ch07)."""

from __future__ import annotations

from mewcode.mcp.client import MCPClient
from mewcode.mcp.manager import MCPManager
from mewcode.mcp.tool_wrapper import MCPToolWrapper

__all__ = ["MCPManager", "MCPClient", "MCPToolWrapper"]
