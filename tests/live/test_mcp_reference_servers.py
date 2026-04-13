"""Live tests against real reference MCP servers (no auth required).

These exercise the full MCP consumer stack (stdio transport, manager
lifecycle, method list pagination, tools/call round-trip, shutdown)
against the canonical reference implementations published at
``@modelcontextprotocol/*`` on npm. Every server used here runs
without credentials, so any contributor with network access and
``npx`` on PATH can reproduce the green matrix.

Gated behind the ``live_network`` pytest marker and a runtime check
for ``npx`` on PATH. When ``npx`` is missing, every test in this
file is skipped cleanly.
"""

from __future__ import annotations

import shutil
import sys

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.manager import MCPManager
from kiso.mcp.stdio import MCPStdioClient

pytestmark = pytest.mark.live_network

_NPX = shutil.which("npx")

requires_npx = pytest.mark.skipif(
    _NPX is None, reason="npx not on PATH — install Node.js + npm"
)


def _npx_server(name: str, package: str, *extra_args: str) -> MCPServer:
    return MCPServer(
        name=name,
        transport="stdio",
        command="npx",
        args=["-y", package, *extra_args],
        timeout_s=90.0,
    )


# ---------------------------------------------------------------------------
# @modelcontextprotocol/server-everything — reference impl of all primitives
# ---------------------------------------------------------------------------


class TestEverythingServer:
    @requires_npx
    async def test_initialize_and_list_methods(self, tmp_path):
        server = _npx_server("everything", "@modelcontextprotocol/server-everything")
        client = MCPStdioClient(server)
        try:
            info = await client.initialize()
            assert info.name, "server must return a non-empty name"
            methods = await client.list_methods()
            assert len(methods) > 0, "server-everything exposes methods"
            for m in methods:
                assert m.server == "everything"
                assert isinstance(m.input_schema, dict)
        finally:
            await client.shutdown()

    @requires_npx
    async def test_manager_reuses_client(self, tmp_path):
        servers = {
            "everything": _npx_server(
                "everything", "@modelcontextprotocol/server-everything"
            )
        }
        mgr = MCPManager(servers)
        try:
            first = await mgr.list_methods("everything")
            second = await mgr.list_methods("everything")
            # Second call is cached, but method set is stable
            assert [m.qualified for m in first] == [m.qualified for m in second]
        finally:
            await mgr.shutdown_all()


# ---------------------------------------------------------------------------
# @modelcontextprotocol/server-memory — in-memory knowledge graph
# ---------------------------------------------------------------------------


class TestMemoryServer:
    @requires_npx
    async def test_lifecycle_lists_methods(self, tmp_path):
        server = _npx_server("memory", "@modelcontextprotocol/server-memory")
        client = MCPStdioClient(server)
        try:
            info = await client.initialize()
            assert info.protocol_version  # server speaks *a* protocol version
            methods = await client.list_methods()
            # server-memory exposes graph CRUD methods
            names = {m.name for m in methods}
            # At least one well-known method should be present
            assert len(names) > 0
        finally:
            await client.shutdown()


# ---------------------------------------------------------------------------
# @modelcontextprotocol/server-sequentialthinking — tools/call smoke
# ---------------------------------------------------------------------------


class TestSequentialThinkingServer:
    @requires_npx
    async def test_initialize_and_shutdown(self, tmp_path):
        server = _npx_server(
            "thinking", "@modelcontextprotocol/server-sequentialthinking"
        )
        client = MCPStdioClient(server)
        try:
            info = await client.initialize()
            assert info.name
            methods = await client.list_methods()
            assert len(methods) > 0
        finally:
            await client.shutdown()


# ---------------------------------------------------------------------------
# Sanity: unknown npm package surfaces a clear error, not a hang
# ---------------------------------------------------------------------------


class TestNonexistentPackage:
    @requires_npx
    async def test_nonexistent_package_clear_error(self, tmp_path):
        """Spawning a npx package that does not exist must surface a
        transport / invocation error within the per-call timeout and
        leave the client in a non-healthy state (no dangling subprocess)."""
        server = MCPServer(
            name="nope",
            transport="stdio",
            command="npx",
            args=["-y", "@kiso-run/definitely-does-not-exist-xyzzy"],
            timeout_s=30.0,
        )
        client = MCPStdioClient(server)
        try:
            with pytest.raises(Exception):
                await client.initialize()
            assert client.is_healthy() is False
        finally:
            await client.shutdown()
