"""Optional auth-required live tests against real MCP servers.

Each test is gated on a specific env var; when the env var is
unset, the test skips cleanly. Contributors with credentials for
any of these services get bonus coverage; those without see
skips. The purpose is to validate that Kiso's MCP client works
with real-world auth flows, not to make CI flaky on missing
credentials.

Supported servers:

- ``BRAVE_API_KEY`` → ``@modelcontextprotocol/server-brave-search``
- ``GOOGLE_MAPS_API_KEY`` → Google Maps Grounding Lite
  (Streamable HTTP, tests the HTTP transport end-to-end)
- ``GITHUB_PERSONAL_ACCESS_TOKEN`` → ``@modelcontextprotocol/server-github``

Env var names match exactly what each MCP server expects
natively, so a contributor who already has one of these keys
set for real usage of the same server gets automatic live
test coverage with zero duplication — one key powers both
production usage and this smoke test.

Do **not** hard-code API keys here. Tests only read from env.
"""

from __future__ import annotations

import os
import shutil

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.stdio import MCPStdioClient
from kiso.mcp.http import MCPStreamableHTTPClient

pytestmark = pytest.mark.live_network


_BRAVE_KEY = os.environ.get("BRAVE_API_KEY")
_GMAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
_GITHUB_PAT = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")


class TestBraveSearch:
    @pytest.mark.skipif(not _BRAVE_KEY, reason="BRAVE_API_KEY not set")
    @pytest.mark.skipif(shutil.which("npx") is None, reason="npx not on PATH")
    async def test_brave_search_smoke(self):
        """Spawn @modelcontextprotocol/server-brave-search via npx and
        run initialize + list_methods + a trivial query."""
        server = MCPServer(
            name="brave",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-brave-search"],
            env={"BRAVE_API_KEY": _BRAVE_KEY},
            timeout_s=60.0,
        )
        client = MCPStdioClient(server)
        try:
            info = await client.initialize()
            assert info.name
            methods = await client.list_methods()
            assert len(methods) > 0
        finally:
            await client.shutdown()


class TestGoogleMapsGroundingLite:
    @pytest.mark.skipif(
        not _GMAPS_KEY, reason="GOOGLE_MAPS_API_KEY not set"
    )
    async def test_google_maps_grounding_smoke(self):
        """HTTP transport smoke test against the real Google Maps
        Grounding Lite hosted MCP endpoint. Validates the
        Streamable HTTP transport end-to-end against a real
        vendor-published server."""
        server = MCPServer(
            name="google-maps",
            transport="http",
            url="https://mapstools.googleapis.com/mcp",
            headers={"X-Goog-Api-Key": _GMAPS_KEY},
            timeout_s=60.0,
        )
        client = MCPStreamableHTTPClient(server)
        try:
            info = await client.initialize()
            assert info.name
            methods = await client.list_methods()
            assert any("search" in m.name.lower() for m in methods)
        finally:
            await client.shutdown()


class TestGitHubServer:
    @pytest.mark.skipif(
        not _GITHUB_PAT, reason="GITHUB_PERSONAL_ACCESS_TOKEN not set"
    )
    @pytest.mark.skipif(shutil.which("npx") is None, reason="npx not on PATH")
    async def test_github_server_smoke(self):
        """Spawn @modelcontextprotocol/server-github via npx with a real
        GitHub PAT and verify initialize + list_methods. Proves the
        stdio transport works end-to-end against the most widely used
        authenticated MCP server in the ecosystem.

        Smoke-only: no real tools/call to avoid coupling the test to
        specific repo content or GitHub API method renames upstream.
        Method-count assertion catches upstream regressions where the
        server exposes an empty (or near-empty) tool list — the real
        server-github exposes ~26 methods.
        """
        server = MCPServer(
            name="github",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": _GITHUB_PAT},
            timeout_s=90.0,
        )
        client = MCPStdioClient(server)
        try:
            info = await client.initialize()
            assert info.name
            methods = await client.list_methods()
            assert len(methods) > 5, (
                f"server-github exposed only {len(methods)} method(s); "
                f"expected >5 (real server has ~26)"
            )
        finally:
            await client.shutdown()
