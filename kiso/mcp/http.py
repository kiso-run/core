"""Streamable HTTP transport for the MCP client.

Implements the current MCP HTTP transport (spec version 2025-06-18):
HTTP POST to a single MCP endpoint URL, with dual content-type
response handling:

- ``application/json``: a single JSON-RPC object as the response body
- ``text/event-stream``: an SSE stream where the final event carries
  the JSON-RPC response, possibly preceded by server-initiated
  notifications

Session management follows the spec: the server assigns an
``Mcp-Session-Id`` on the initialize response, the client includes it
in every subsequent request header, and the client transparently
re-initializes when the server returns HTTP 404 for a stale session.

The ``MCP-Protocol-Version`` header is sent on every request so the
server knows which spec version the client targets.

Legacy HTTP+SSE transport (MCP spec 2024-11-05) is deliberately NOT
supported. On HTTP 405 from the new transport's POST, the client
emits a clear error naming the limitation and suggesting the server's
stdio mode (if available) — no silent fallback, no hidden complexity.

Protocol-internal JSON-RPC method names (``initialize``,
``tools/list``, ``tools/call``, ``notifications/cancelled``,
``notifications/initialized``) appear only here and in the stdio
transport, per the MCP spec. All Kiso-facing vocabulary uses
"method".
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import httpx

from kiso.mcp.client import MCPClient
from kiso.mcp.config import MCPServer
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPError,
    MCPInvocationError,
    MCPMethod,
    MCPProtocolError,
    MCPServerInfo,
    MCPTransportError,
)
from kiso.mcp.stdio import _build_call_result  # reuse the shared renderer

log = logging.getLogger(__name__)

CLIENT_PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "kiso"
CLIENT_VERSION = "0.9.0"


class MCPStreamableHTTPClient(MCPClient):
    """Concrete MCP client over Streamable HTTP transport."""

    def __init__(
        self,
        server: MCPServer,
        *,
        _http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        if server.transport != "http":
            raise ValueError(
                f"MCPStreamableHTTPClient requires transport='http', got {server.transport!r}"
            )
        if not server.url:
            raise ValueError("MCPStreamableHTTPClient requires a non-empty url")
        self._server = server
        self._session_id: str | None = None
        self._initialized = False
        self._shut_down = False
        self._next_id = 1
        self._http_factory = _http_client_factory or self._default_http_factory
        self._http: httpx.AsyncClient | None = None
        self._server_info: MCPServerInfo | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def initialize(self) -> MCPServerInfo:
        if self._initialized:
            raise MCPProtocolError("client already initialized")
        self._shut_down = False
        self._http = self._http_factory()

        response_body, headers = await self._post_rpc(
            "initialize",
            {
                "protocolVersion": CLIENT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": "Kiso MCP Client",
                    "version": CLIENT_VERSION,
                },
            },
            with_session=False,
        )

        if "error" in response_body:
            err = response_body["error"]
            msg = err.get("message", "initialize rejected")
            raise MCPProtocolError(f"initialize: {msg}")
        if "result" not in response_body:
            raise MCPProtocolError(
                f"initialize: response has no result: {response_body!r}"
            )
        session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        if not session_id:
            raise MCPProtocolError(
                "initialize: server did not return an Mcp-Session-Id header"
            )
        self._session_id = session_id

        result = response_body["result"]
        server_info = result.get("serverInfo") or {}
        info = MCPServerInfo(
            name=server_info.get("name", "unknown"),
            title=server_info.get("title"),
            version=server_info.get("version", "0.0.0"),
            protocol_version=result.get("protocolVersion", ""),
            capabilities=result.get("capabilities") or {},
            instructions=result.get("instructions"),
        )
        self._server_info = info

        # notifications/initialized — fire and forget (202 Accepted)
        await self._post_notification("notifications/initialized", {})

        self._initialized = True
        return info

    async def list_methods(self) -> list[MCPMethod]:
        self._require_initialized()
        methods: list[MCPMethod] = []
        cursor: str | None = None
        while True:
            params: dict = {}
            if cursor is not None:
                params["cursor"] = cursor
            response_body, _ = await self._post_rpc("tools/list", params)
            if "error" in response_body:
                raise MCPInvocationError(
                    f"tools/list failed: {response_body['error']}"
                )
            result = response_body.get("result") or {}
            for raw in result.get("tools") or []:
                methods.append(
                    MCPMethod(
                        server=self._server.name,
                        name=raw.get("name", ""),
                        title=raw.get("title"),
                        description=raw.get("description", ""),
                        input_schema=raw.get("inputSchema") or {},
                        output_schema=raw.get("outputSchema"),
                        annotations=raw.get("annotations"),
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return methods

    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        self._require_initialized()
        response_body, _ = await self._post_rpc(
            "tools/call", {"name": name, "arguments": args or {}}
        )
        if "error" in response_body:
            err = response_body["error"]
            msg = err.get("message") or str(err)
            raise MCPInvocationError(f"tools/call {name}: {msg}")
        result = response_body.get("result") or {}
        return _build_call_result(result)

    async def cancel(self, request_id: Any) -> None:
        if self._http is None or self._shut_down:
            return
        try:
            await self._post_notification(
                "notifications/cancelled",
                {"requestId": request_id, "reason": "client cancelled"},
            )
        except Exception as e:  # noqa: BLE001
            log.debug("mcp[%s] cancel notification failed: %s", self._server.name, e)

    async def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        if self._http is None:
            self._initialized = False
            return
        if self._session_id is not None:
            try:
                headers = self._base_headers(with_session=True)
                await self._http.delete(
                    self._server.url, headers=headers, timeout=self._server.timeout_s
                )
            except Exception as e:  # noqa: BLE001
                log.debug("mcp[%s] shutdown DELETE failed: %s", self._server.name, e)
        try:
            await self._http.aclose()
        except Exception as e:  # noqa: BLE001
            log.debug("mcp[%s] aclose failed: %s", self._server.name, e)
        self._http = None
        self._session_id = None
        self._initialized = False

    def is_healthy(self) -> bool:
        return self._initialized and not self._shut_down

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _default_http_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient()

    def _require_initialized(self) -> None:
        if self._shut_down:
            raise MCPProtocolError("client has been shut down")
        if not self._initialized:
            raise MCPProtocolError("client not initialized")

    def _base_headers(self, *, with_session: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": CLIENT_PROTOCOL_VERSION,
        }
        for k, v in self._server.headers.items():
            headers[k] = v
        if with_session and self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post_rpc(
        self,
        method: str,
        params: dict,
        *,
        with_session: bool = True,
        _retried: bool = False,
    ) -> tuple[dict, dict[str, str]]:
        """POST a JSON-RPC request and return (response_json, headers).

        Handles the dual content-type response: single ``application/json``
        or a ``text/event-stream`` SSE stream whose final event carries
        the response. Session expiry (404) triggers exactly one
        transparent re-initialization + retry.
        """
        assert self._http is not None
        req_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        headers = self._base_headers(with_session=with_session)
        try:
            response = await self._http.post(
                self._server.url,
                content=json.dumps(payload).encode("utf-8"),
                headers=headers,
                timeout=self._server.timeout_s,
            )
        except httpx.HTTPError as e:
            raise MCPTransportError(
                f"mcp[{self._server.name}] http post failed: {e}"
            ) from e

        if response.status_code == 405:
            raise MCPTransportError(
                f"mcp[{self._server.name}] server returned 405 Method Not Allowed "
                f"on the Streamable HTTP endpoint. This usually means the server "
                f"only speaks the legacy HTTP+SSE transport (MCP spec 2024-11-05), "
                f"which Kiso does not support. If the server offers a stdio mode, "
                f"use that instead."
            )
        if response.status_code == 404 and with_session and not _retried:
            # Session expired — re-init and retry exactly once.
            log.info(
                "mcp[%s] session expired (404), re-initializing",
                self._server.name,
            )
            self._initialized = False
            self._session_id = None
            await self.initialize()
            return await self._post_rpc(
                method, params, with_session=True, _retried=True
            )
        if response.status_code >= 400:
            raise MCPTransportError(
                f"mcp[{self._server.name}] http {response.status_code}: "
                f"{response.text[:200]}"
            )

        content_type = response.headers.get("content-type", "")
        body_bytes = response.content

        if "text/event-stream" in content_type:
            data = _parse_sse_final_message(body_bytes)
        else:
            data = body_bytes

        if not data:
            raise MCPProtocolError(
                f"mcp[{self._server.name}] {method}: empty response body"
            )
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            raise MCPProtocolError(
                f"mcp[{self._server.name}] {method}: malformed JSON response: {e}"
            ) from e

        return parsed, dict(response.headers)

    async def _post_notification(self, method: str, params: dict) -> None:
        assert self._http is not None
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            await self._http.post(
                self._server.url,
                content=json.dumps(payload).encode("utf-8"),
                headers=self._base_headers(with_session=True),
                timeout=self._server.timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            log.debug(
                "mcp[%s] notification %s failed: %s",
                self._server.name, method, e,
            )


def _parse_sse_final_message(body: bytes) -> bytes:
    """Extract the last ``data:`` payload from an SSE body.

    The spec says: in response to a POST, the server may open an SSE
    stream; the final event in that stream carries the JSON-RPC
    response. We collect all ``data:`` lines and return the content of
    the last SSE event.

    Simple parser: split on blank-line separators (``\\n\\n``), take the
    last non-empty block, extract all ``data:`` lines from it,
    concatenate, return the resulting bytes. If nothing matches,
    returns empty bytes and the caller raises a protocol error.
    """
    text = body.decode("utf-8", errors="replace")
    # Normalise CRLF → LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in text.split("\n\n") if b.strip()]
    if not blocks:
        return b""
    last_block = blocks[-1]
    data_lines = []
    for line in last_block.split("\n"):
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return "".join(data_lines).encode("utf-8") if data_lines else b""
