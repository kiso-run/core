"""M1544 — HTTP MCP `sampling/createMessage` bidirectional dispatch.

Encodes the invariant: when the http config allows sampling and the
server sends an SSE stream with one or more server-to-client
`sampling/createMessage` requests interleaved before the final
response, the kiso client dispatches each through the
`sampler` handler and POSTs the response back.

We test:

1. `_build_client_capabilities` advertises `sampling` when the bound
   Config has `mcp_sampling_enabled = True`.
2. A helper that splits an SSE body into all events (not just the
   final one) handles multi-event streams.
3. A helper that classifies a parsed JSON-RPC frame as a
   server-to-client request vs. the response-to-our-POST.
"""

from __future__ import annotations

import json


class TestAdvertisesSampling:
    def test_returns_sampling_when_enabled(self) -> None:
        from unittest.mock import MagicMock

        from kiso.mcp.config import MCPServer
        from kiso.mcp.http import MCPStreamableHTTPClient as MCPHttpClient

        config = MagicMock()
        config.settings = {"mcp_sampling_enabled": True}
        server = MCPServer(
            name="remote",
            transport="http",
            url="https://example.invalid/mcp",
            enabled=True,
            headers={},
            timeout_s=10.0,
        )
        client = MCPHttpClient(server, config=config)
        assert client.advertises_sampling is True

    def test_returns_nothing_when_disabled(self) -> None:
        from unittest.mock import MagicMock

        from kiso.mcp.config import MCPServer
        from kiso.mcp.http import MCPStreamableHTTPClient as MCPHttpClient

        config = MagicMock()
        config.settings = {"mcp_sampling_enabled": False}
        server = MCPServer(
            name="remote",
            transport="http",
            url="https://example.invalid/mcp",
            enabled=True,
            headers={},
            timeout_s=10.0,
        )
        client = MCPHttpClient(server, config=config)
        assert client.advertises_sampling is False

    def test_returns_nothing_with_no_config(self) -> None:
        from kiso.mcp.config import MCPServer
        from kiso.mcp.http import MCPStreamableHTTPClient as MCPHttpClient

        server = MCPServer(
            name="remote",
            transport="http",
            url="https://example.invalid/mcp",
            enabled=True,
            headers={},
            timeout_s=10.0,
        )
        client = MCPHttpClient(server, config=None)
        assert client.advertises_sampling is False


class TestParseSseAllEvents:
    def test_single_event(self) -> None:
        from kiso.mcp.http import _parse_sse_events

        body = b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        events = _parse_sse_events(body)
        assert events == [b'{"jsonrpc":"2.0","id":1,"result":{}}']

    def test_multi_event_stream(self) -> None:
        from kiso.mcp.http import _parse_sse_events

        body = (
            b'data: {"jsonrpc":"2.0","id":"s1","method":"sampling/createMessage","params":{}}\n\n'
            b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        )
        events = _parse_sse_events(body)
        assert len(events) == 2
        first = json.loads(events[0])
        second = json.loads(events[1])
        assert first["method"] == "sampling/createMessage"
        assert second["id"] == 1
        assert "result" in second


class TestClassifyFrame:
    def test_server_request_has_method_no_result_or_error(self) -> None:
        from kiso.mcp.http import _is_server_request

        frame = {
            "jsonrpc": "2.0",
            "id": "s1",
            "method": "sampling/createMessage",
            "params": {},
        }
        assert _is_server_request(frame) is True

    def test_our_response_has_result(self) -> None:
        from kiso.mcp.http import _is_server_request

        frame = {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert _is_server_request(frame) is False

    def test_our_response_has_error(self) -> None:
        from kiso.mcp.http import _is_server_request

        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -1, "message": "bad"},
        }
        assert _is_server_request(frame) is False

    def test_notification_is_not_our_response(self) -> None:
        """Notifications (no id) don't collide with request/response."""
        from kiso.mcp.http import _is_server_request

        frame = {"jsonrpc": "2.0", "method": "notify/something", "params": {}}
        # A notification has no id so it's not a request-for-reply. Treat
        # it as not-a-server-request for response dispatch purposes.
        assert _is_server_request(frame) is False
