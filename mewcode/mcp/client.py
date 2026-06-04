"""Single-server MCP client (ch07).

Wraps one external MCP server's session: connect (stdio or Streamable HTTP) →
list_tools → call_tool → close. Built on the official `mcp` SDK; the whole
transport + session lifecycle is owned by one ``AsyncExitStack`` so teardown is
a single ``aclose()``.
"""

from __future__ import annotations

import logging

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from contextlib import AsyncExitStack

from mewcode.config import (
    MCPServerConfig,
    build_child_env,
    resolve_env_vars,
)

logger = logging.getLogger(__name__)


class MCPClient:
    """A live handle to one MCP server."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive and self._session is not None

    async def connect(self) -> None:
        """Open transport, create a session, perform the MCP initialize handshake.

        On any failure the partially-built ``AsyncExitStack`` is rolled back so
        no subprocess / socket leaks.
        """
        self._stack = AsyncExitStack()
        try:
            if self.config.is_stdio:
                read, write = await self._connect_stdio()
            else:
                read, write = await self._connect_http()
            session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self._session = session
            self._alive = True
        except BaseException:
            await self._cleanup_stack()
            self._alive = False
            raise

    async def _connect_stdio(self):
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=build_child_env(resolve_env_vars(self.config.env)),
        )
        # stdio_client yields (read_stream, write_stream).
        read, write = await self._stack.enter_async_context(stdio_client(params))
        return read, write

    async def _connect_http(self):
        headers = resolve_env_vars(self.config.headers)
        # streamablehttp_client yields (read, write, get_session_id_callback).
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(self.config.url, headers=headers)
        )
        return read, write

    async def list_tools(self) -> list[types.Tool]:
        assert self._session is not None, "not connected"
        return (await self._session.list_tools()).tools

    async def call_tool(self, name: str, arguments: dict) -> types.CallToolResult:
        assert self._session is not None, "not connected"
        return await self._session.call_tool(name, arguments)

    async def close(self) -> None:
        self._alive = False
        await self._cleanup_stack()

    async def _cleanup_stack(self) -> None:
        """Close the exit stack, swallowing the anyio 'cancel scope' race.

        Closing an anyio-backed stack from a task other than the one that
        opened it raises RuntimeError('... cancel scope ...'); this is a known
        SDK shutdown race and is safe to ignore.
        """
        stack, self._stack = self._stack, None
        self._session = None
        if stack is None:
            return
        try:
            await stack.aclose()
        except RuntimeError as e:
            if "cancel scope" not in str(e):
                logger.debug("MCP %r stack close RuntimeError: %s", self.name, e)
        except Exception as e:  # noqa: BLE001 — teardown must not raise
            logger.debug("MCP %r stack close error: %s", self.name, e)
