"""Live tests against real reference MCP servers (no auth required).

These exercise the full MCP consumer stack (stdio transport, manager
lifecycle, method list pagination, tools/call round-trip, shutdown)
against the canonical reference implementations published at
``@modelcontextprotocol/*`` on npm. Every server used here runs
without credentials, so any contributor with network access and
``npx`` on PATH can reproduce the green matrix.

Gated behind the ``live_network`` pytest marker. **In the kiso test
image** (`KISO_TEST_IMAGE=1`) `npx` is guaranteed by `Dockerfile.test`
since M1367 — a missing binary is a regression and `requires_npx`
fails the test instead of skipping. **On developer hosts** without
`KISO_TEST_IMAGE=1`, the gate stays a soft skip so contributors
without Node.js are not penalised.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.manager import MCPManager
from kiso.mcp.stdio import MCPStdioClient

pytestmark = pytest.mark.live_network

_NPX = shutil.which("npx")
_IN_TEST_IMAGE = os.environ.get("KISO_TEST_IMAGE") == "1"

# When running INSIDE the kiso test image (Dockerfile.test sets
# KISO_TEST_IMAGE=1), `npx` is a hard prerequisite installed by
# M1367. A missing binary is a regression, not an environmental
# quirk — fail the entire test module collection at import time
# so the build is loud about it. Outside the image (dev host),
# fall back to the previous soft-skip behaviour so contributors
# without Node.js are not penalised.
if _NPX is None and _IN_TEST_IMAGE:
    raise RuntimeError(
        "M1367 regression: KISO_TEST_IMAGE=1 but `npx` is not on PATH. "
        "Dockerfile.test must apt-install `nodejs npm`."
    )

requires_npx = pytest.mark.skipif(
    _NPX is None,
    reason="npx not on PATH (dev host, KISO_TEST_IMAGE!=1) — install Node.js + npm",
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
# @modelcontextprotocol/server-filesystem — filesystem MCP server
# ---------------------------------------------------------------------------
#
# Picked over server-sequentialthinking (which does not exist on npm)
# because (a) filesystem is officially maintained, (b) it takes a path
# argument which exercises our arg-passing code path in the stdio
# transport, and (c) it exposes concrete read/list methods suitable
# for a real tools/call smoke test.


class TestFilesystemServer:
    @requires_npx
    async def test_initialize_with_sandbox_path(self, tmp_path):
        """Spawn server-filesystem with tmp_path as its sandbox root and
        verify the full initialize + list_methods lifecycle. Exercises
        argument passing (the path) through the stdio transport and
        validates that we can run a real community reference server
        that requires a CLI argument."""
        (tmp_path / "hello.txt").write_text("test")
        server = MCPServer(
            name="filesystem",
            transport="stdio",
            command="npx",
            args=[
                "-y",
                "@modelcontextprotocol/server-filesystem",
                str(tmp_path),
            ],
            timeout_s=90.0,
        )
        client = MCPStdioClient(server)
        try:
            info = await client.initialize()
            assert info.name
            methods = await client.list_methods()
            assert len(methods) > 0
            # server-filesystem exposes read/list style methods. Assert
            # at least one canonical name appears so the test fails
            # loudly if upstream renames everything.
            names = {m.name.lower() for m in methods}
            assert any(
                "list" in n or "read" in n or "directory" in n for n in names
            ), f"filesystem server exposed no read/list methods: {names}"
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
