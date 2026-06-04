"""Multi-server MCP orchestration (ch07).

`MCPManager` connects every configured server at startup, registers each
server's tools into the global `ToolRegistry` (as `MCPToolWrapper`s), and tears
everything down on exit. A single server failing only logs a warning and is
skipped — it never blocks the others or aborts startup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mewcode.mcp.client import MCPClient
from mewcode.mcp.tool_wrapper import MCPToolWrapper

if TYPE_CHECKING:
    from mewcode.config import MCPServerConfig
    from mewcode.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class MCPManager:
    """Owns the set of MCP server connections for the session."""

    def __init__(self) -> None:
        self._configs: dict[str, "MCPServerConfig"] = {}
        self._clients: dict[str, MCPClient] = {}

    def load_configs(self, configs: list["MCPServerConfig"]) -> None:
        for cfg in configs:
            self._configs[cfg.name] = cfg

    async def register_all_tools(self, registry: "ToolRegistry") -> list[str]:
        """Connect each server and register its tools. Returns failure messages.

        Failure isolation: one server raising during connect / list_tools only
        appends to ``errors`` (and warns); the rest still register.
        """
        errors: list[str] = []
        for name, cfg in self._configs.items():
            try:
                client = MCPClient(cfg)
                await client.connect()
                tools = await client.list_tools()
                self._clients[name] = client
                for tool_def in tools:
                    registry.register(MCPToolWrapper(name, tool_def, client))
                logger.info(
                    "MCP server %r connected (%d tools)", name, len(tools)
                )
            except Exception as e:  # noqa: BLE001 — isolate per-server failure
                logger.warning("MCP server %r failed to connect: %s", name, e)
                errors.append(f"{name}: {e}")
        return errors

    async def get_client(self, name: str) -> MCPClient:
        """Return a live client for ``name``, (re)connecting lazily if needed."""
        client = self._clients.get(name)
        if client is not None and client.is_alive:
            return client
        cfg = self._configs[name]
        client = MCPClient(cfg)
        await client.connect()
        self._clients[name] = client
        return client

    async def shutdown(self) -> None:
        """Close every client. Idempotent; errors only logged."""
        for name, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception as e:  # noqa: BLE001 — teardown must not raise
                logger.debug("MCP server %r shutdown error: %s", name, e)
        self._clients.clear()

    @property
    def tool_names(self) -> list[str]:
        return [c.name for c in self._configs.values()]
