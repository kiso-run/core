"""Unit tests for ``MCPStreamableHTTPClient``.

Uses ``tests/fixtures/mcp_mock_http_server.py`` — an in-process
FastAPI app served via ``httpx.ASGITransport`` so no socket is
bound and there is no teardown race.
"""

from __future__ import annotations

import httpx
import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.http import MCPStreamableHTTPClient
from kiso.mcp.schemas import (
    MCPInvocationError,
    MCPProtocolError,
    MCPTransportError,
)
from tests.fixtures.mcp_mock_http_server import make_app


def _server(url: str = "http://mock/mcp") -> MCPServer:
    return MCPServer(
        name="mock",
        transport="http",
        url=url,
        headers={},
        auth=None,
        enabled=True,
        timeout_s=5.0,
    )


def _client_for_app(app, **server_overrides) -> MCPStreamableHTTPClient:
    """Build a client whose HTTP calls are routed via ASGITransport
    to the given FastAPI app. The client uses httpx under the hood;
    we pass a custom AsyncClient via the client's internal injection
    hook so the mock app handles the requests directly."""
    transport = httpx.ASGITransport(app=app)
    server = _server(**server_overrides)
    client = MCPStreamableHTTPClient(
        server, _http_client_factory=lambda: httpx.AsyncClient(transport=transport)
    )
    return client


# ---------------------------------------------------------------------------
# Initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    async def test_happy_json(self):
        client = _client_for_app(make_app("happy_json"))
        info = await client.initialize()
        assert info.name == "mock-http-mcp"
        assert info.protocol_version == "2025-06-18"
        assert client.session_id is not None
        await client.shutdown()

    async def test_happy_sse(self):
        """Server returns text/event-stream for initialize; client must
        parse the SSE stream and extract the response event."""
        client = _client_for_app(make_app("happy_sse"))
        info = await client.initialize()
        assert info.name == "mock-http-mcp"
        assert client.session_id is not None
        await client.shutdown()

    async def test_double_init_rejected(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        with pytest.raises(MCPProtocolError, match="already initialized"):
            await client.initialize()
        await client.shutdown()

    async def test_legacy_405_clear_error_no_fallback(self):
        """Server only speaks the deprecated HTTP+SSE transport; Kiso
        does NOT implement the fallback, must emit a clear error
        pointing users at stdio alternatives."""
        client = _client_for_app(make_app("legacy_405"))
        with pytest.raises(MCPTransportError, match="legacy"):
            await client.initialize()

    async def test_protocol_version_mismatch(self):
        client = _client_for_app(make_app("protocol_version_mismatch"))
        with pytest.raises(MCPProtocolError):
            await client.initialize()


# ---------------------------------------------------------------------------
# list_methods
# ---------------------------------------------------------------------------


class TestListMethods:
    async def test_happy_json(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        methods = await client.list_methods()
        assert len(methods) == 1
        assert methods[0].name == "ping"
        assert methods[0].qualified == "mock:ping"
        await client.shutdown()

    async def test_happy_sse(self):
        client = _client_for_app(make_app("happy_sse"))
        await client.initialize()
        methods = await client.list_methods()
        assert [m.name for m in methods] == ["ping"]
        await client.shutdown()

    async def test_list_before_initialize(self):
        client = _client_for_app(make_app("happy_json"))
        with pytest.raises(MCPProtocolError, match="not initialized"):
            await client.list_methods()


# ---------------------------------------------------------------------------
# call_method
# ---------------------------------------------------------------------------


class TestCallMethod:
    async def test_happy(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        result = await client.call_method("ping", {"echo": "world"})
        assert result.is_error is False
        assert "pong:world" in result.stdout_text
        await client.shutdown()

    async def test_session_expired_triggers_reinit(self):
        """After session_id expires (404), the client must transparently
        re-initialize and retry the call with a new session-id."""
        client = _client_for_app(make_app("session_expires"))
        await client.initialize()
        first_session = client.session_id
        result1 = await client.call_method("ping", {"echo": "1"})
        assert result1.is_error is False
        # Second call triggers session expiry → client re-inits
        result2 = await client.call_method("ping", {"echo": "2"})
        assert result2.is_error is False
        assert client.session_id != first_session
        await client.shutdown()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_shutdown_sends_delete(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        await client.shutdown()
        # Further calls must fail
        with pytest.raises(MCPProtocolError, match="shut down|not initialized"):
            await client.list_methods()

    async def test_shutdown_idempotent(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        await client.shutdown()
        await client.shutdown()

    async def test_shutdown_before_initialize(self):
        client = _client_for_app(make_app("happy_json"))
        await client.shutdown()


# ---------------------------------------------------------------------------
# is_healthy
# ---------------------------------------------------------------------------


class TestIsHealthy:
    async def test_before_init(self):
        client = _client_for_app(make_app("happy_json"))
        assert client.is_healthy() is False

    async def test_after_init(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        assert client.is_healthy() is True
        await client.shutdown()

    async def test_after_shutdown(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        await client.shutdown()
        assert client.is_healthy() is False
