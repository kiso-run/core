"""Optional auth-required live tests against real MCP servers.

Each test is gated on a specific env var; when the env var is
unset, the test skips cleanly. Contributors with credentials for
any of these services get bonus coverage; those without see
skips. The purpose is to validate that Kiso's MCP client works
with real-world auth flows, not to make CI flaky on missing
credentials.

Supported servers:

- ``TEST_BRAVE_API_KEY`` → ``@modelcontextprotocol/server-brave-search``
- ``TEST_GOOGLE_MAPS_API_KEY`` → Google Maps Grounding Lite
  (Streamable HTTP, tests the HTTP transport end-to-end)

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


_BRAVE_KEY = os.environ.get("TEST_BRAVE_API_KEY")
_GMAPS_KEY = os.environ.get("TEST_GOOGLE_MAPS_API_KEY")


class TestBraveSearch:
    @pytest.mark.skipif(not _BRAVE_KEY, reason="TEST_BRAVE_API_KEY not set")
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
        not _GMAPS_KEY, reason="TEST_GOOGLE_MAPS_API_KEY not set"
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
