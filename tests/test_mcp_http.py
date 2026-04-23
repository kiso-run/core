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
    MCPPrompt,
    MCPPromptResult,
    MCPProtocolError,
    MCPResource,
    MCPResourceContent,
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

    async def test_stateless_server_no_session_id(self):
        """Server omits the Mcp-Session-Id header on initialize.

        Per MCP spec 2025-06-18, session IDs are optional. Stateless
        hosted servers (e.g. Google Maps Grounding Lite) do not issue
        them. The client must:
          - accept the initialize response without raising
          - expose `session_id == None`
          - run list_methods + call_method successfully without
            sending any Mcp-Session-Id header
          - shut down cleanly (no DELETE attempted)
        """
        client = _client_for_app(make_app("stateless"))
        info = await client.initialize()
        assert info.name == "mock-http-mcp"
        assert client.session_id is None
        methods = await client.list_methods()
        assert [m.name for m in methods] == ["ping"]
        result = await client.call_method("ping", {"echo": "hi"})
        assert result.is_error is False
        assert "pong:hi" in result.stdout_text
        await client.shutdown()

    async def test_stateful_server_still_echoes_session_id(self):
        """Regression guard: stateful servers must still receive the
        Mcp-Session-Id header on every request after initialize."""
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        assert client.session_id is not None
        # list_methods and call_method only succeed if the mock sees
        # the session id header (otherwise it returns 404).
        methods = await client.list_methods()
        assert methods
        await client.shutdown()


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


class TestListResources:
    async def test_happy(self):
        client = _client_for_app(make_app("resources_happy"))
        await client.initialize()
        resources = await client.list_resources()
        uris = {r.uri for r in resources}
        assert uris == {"kiso://http/log", "kiso://http/row/7"}
        assert all(r.server == "mock" for r in resources)
        await client.shutdown()

    async def test_empty_when_no_capability(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        resources = await client.list_resources()
        assert resources == []
        await client.shutdown()

    async def test_list_before_initialize_fails(self):
        client = _client_for_app(make_app("resources_happy"))
        with pytest.raises(MCPProtocolError):
            await client.list_resources()


class TestReadResource:
    async def test_read_text(self):
        client = _client_for_app(make_app("resources_happy"))
        await client.initialize()
        blocks = await client.read_resource("kiso://http/log")
        assert len(blocks) == 1
        assert isinstance(blocks[0], MCPResourceContent)
        assert blocks[0].text == "http-body-of:kiso://http/log"
        assert blocks[0].mime_type == "text/plain"
        await client.shutdown()

    async def test_read_error_surfaces(self):
        client = _client_for_app(make_app("resources_error"))
        await client.initialize()
        with pytest.raises(MCPInvocationError):
            await client.read_resource("kiso://missing")
        await client.shutdown()


class TestListPrompts:
    async def test_happy(self):
        client = _client_for_app(make_app("prompts_happy"))
        await client.initialize()
        prompts = await client.list_prompts()
        assert [p.name for p in prompts] == ["greet"]
        assert prompts[0].server == "mock"
        arg_names = {a.name for a in prompts[0].arguments}
        assert arg_names == {"name"}
        await client.shutdown()

    async def test_empty_when_no_capability(self):
        client = _client_for_app(make_app("happy_json"))
        await client.initialize()
        prompts = await client.list_prompts()
        assert prompts == []
        await client.shutdown()

    async def test_list_before_initialize_fails(self):
        client = _client_for_app(make_app("prompts_happy"))
        with pytest.raises(MCPProtocolError):
            await client.list_prompts()


class TestGetPrompt:
    async def test_get_rendered(self):
        client = _client_for_app(make_app("prompts_happy"))
        await client.initialize()
        rendered = await client.get_prompt("greet", {"name": "Paolo"})
        assert isinstance(rendered, MCPPromptResult)
        assert rendered.messages[0].text == "Hello Paolo!"
        await client.shutdown()

    async def test_get_error_surfaces(self):
        client = _client_for_app(make_app("prompts_error"))
        await client.initialize()
        with pytest.raises(MCPInvocationError):
            await client.get_prompt("greet", {"name": "x"})
        await client.shutdown()


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
