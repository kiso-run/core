"""Streamable HTTP transport for the MCP client.

Implements the current MCP HTTP transport (spec version 2025-06-18):
HTTP POST to a single MCP endpoint URL, with dual content-type
response handling:

- ``application/json``: a single JSON-RPC object as the response body
- ``text/event-stream``: an SSE stream where the final event carries
  the JSON-RPC response, possibly preceded by server-initiated
  notifications

Session management follows the spec: the server MAY assign an
``Mcp-Session-Id`` on the initialize response; when present the client
includes it in every subsequent request header, and transparently
re-initializes when the server returns HTTP 404 for a stale session.
Stateless servers that omit the session-id header are supported: the
client runs without a session id and sends no session header on
subsequent requests.

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
    MCPPrompt,
    MCPPromptResult,
    MCPProtocolError,
    MCPResource,
    MCPResourceContent,
    MCPServerInfo,
    MCPTransportError,
)
from kiso.mcp.stdio import (
    _build_call_result,
    _build_prompt,
    _build_prompt_result,
    _build_resource_blocks,
)  # reuse renderers

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
        config: Any | None = None,
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
        self._auth_token: str | None = None
        self._config = config

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

        # Resolve auth before connecting (M1374).
        from kiso.mcp.auth import resolve_auth
        self._auth_token = resolve_auth(self._server)

        self._http = self._http_factory()

        response_body, headers = await self._post_rpc(
            "initialize",
            {
                "protocolVersion": CLIENT_PROTOCOL_VERSION,
                "capabilities": self._build_client_capabilities(),
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
        # Session id is optional per MCP spec 2025-06-18. Stateless
        # servers may omit the header entirely; in that case we run
        # without a session id and never emit the header on follow-up
        # requests.
        self._session_id = headers.get("mcp-session-id") or headers.get(
            "Mcp-Session-Id"
        ) or None

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

    async def list_resources(self) -> list[MCPResource]:
        self._require_initialized()
        caps = (self._server_info.capabilities if self._server_info else {}) or {}
        if "resources" not in caps:
            return []
        resources: list[MCPResource] = []
        cursor: str | None = None
        while True:
            params: dict = {}
            if cursor is not None:
                params["cursor"] = cursor
            response_body, _ = await self._post_rpc("resources/list", params)
            if "error" in response_body:
                raise MCPInvocationError(
                    f"resources/list failed: {response_body['error']}"
                )
            result = response_body.get("result") or {}
            for raw in result.get("resources") or []:
                resources.append(
                    MCPResource(
                        server=self._server.name,
                        uri=raw.get("uri", ""),
                        name=raw.get("name", ""),
                        description=raw.get("description", ""),
                        mime_type=raw.get("mimeType"),
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return resources

    async def read_resource(self, uri: str) -> list[MCPResourceContent]:
        self._require_initialized()
        response_body, _ = await self._post_rpc(
            "resources/read", {"uri": uri}
        )
        if "error" in response_body:
            err = response_body["error"]
            msg = err.get("message") or str(err)
            raise MCPInvocationError(f"resources/read {uri}: {msg}")
        result = response_body.get("result") or {}
        return _build_resource_blocks(result)

    async def list_prompts(self) -> list[MCPPrompt]:
        self._require_initialized()
        caps = (self._server_info.capabilities if self._server_info else {}) or {}
        if "prompts" not in caps:
            return []
        prompts: list[MCPPrompt] = []
        cursor: str | None = None
        while True:
            params: dict = {}
            if cursor is not None:
                params["cursor"] = cursor
            response_body, _ = await self._post_rpc("prompts/list", params)
            if "error" in response_body:
                raise MCPInvocationError(
                    f"prompts/list failed: {response_body['error']}"
                )
            result = response_body.get("result") or {}
            for raw in result.get("prompts") or []:
                prompts.append(_build_prompt(self._server.name, raw))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return prompts

    async def get_prompt(self, name: str, args: dict) -> MCPPromptResult:
        self._require_initialized()
        response_body, _ = await self._post_rpc(
            "prompts/get", {"name": name, "arguments": args or {}}
        )
        if "error" in response_body:
            err = response_body["error"]
            msg = err.get("message") or str(err)
            raise MCPInvocationError(f"prompts/get {name}: {msg}")
        result = response_body.get("result") or {}
        return _build_prompt_result(result)

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

    @property
    def advertises_sampling(self) -> bool:
        return "sampling" in self._build_client_capabilities()

    def _build_client_capabilities(self) -> dict:
        """Negotiate ``sampling`` when the bound config allows it.

        The SSE event pump dispatches server-initiated
        ``sampling/createMessage`` requests through
        :func:`kiso.mcp.sampling.handle_sampling_request` and POSTs
        the response back as a notification — same handler used by
        the stdio transport.
        """
        if self._config is None:
            return {}
        try:
            enabled = bool(self._config.settings.get("mcp_sampling_enabled", True))
        except Exception:  # noqa: BLE001 — permissive on odd configs
            return {}
        return {"sampling": {}} if enabled else {}

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
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
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
            # An SSE response may interleave server-to-client
            # JSON-RPC requests (e.g. ``sampling/createMessage``)
            # before the final response-to-our-POST. Dispatch each
            # server request and identify our response by matching
            # JSON-RPC ``id``.
            events = _parse_sse_events(body_bytes)
            parsed = await self._consume_sse_events(events, req_id, method)
            return parsed, dict(response.headers)

        if not body_bytes:
            raise MCPProtocolError(
                f"mcp[{self._server.name}] {method}: empty response body"
            )
        try:
            parsed = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            raise MCPProtocolError(
                f"mcp[{self._server.name}] {method}: malformed JSON response: {e}"
            ) from e

        return parsed, dict(response.headers)

    async def _consume_sse_events(
        self,
        events: list[bytes],
        req_id: int,
        method: str,
    ) -> dict:
        """Walk every SSE event; dispatch server requests; return our response."""
        from kiso.mcp.sampling import SAMPLING_METHOD, handle_sampling_request

        our_response: dict | None = None
        for raw in events:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                log.debug(
                    "mcp[%s] discarded malformed SSE event: %r",
                    self._server.name, raw[:200],
                )
                continue
            if _is_server_request(frame):
                srv_method = frame.get("method")
                srv_id = frame.get("id")
                if srv_method == SAMPLING_METHOD and self._config is not None:
                    try:
                        response = await handle_sampling_request(
                            self._config, frame,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.exception(
                            "mcp[%s] sampling handler crashed",
                            self._server.name,
                        )
                        response = {
                            "jsonrpc": "2.0",
                            "id": srv_id,
                            "error": {
                                "code": -32603,
                                "message": f"handler crashed: {exc}",
                            },
                        }
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": srv_id,
                        "error": {
                            "code": -32601,
                            "message": f"method not found: {srv_method!r}",
                        },
                    }
                await self._post_raw_payload(
                    response,
                    descriptor=f"sampling-response id={srv_id}",
                )
                continue
            if frame.get("id") == req_id:
                our_response = frame
        if our_response is None:
            raise MCPProtocolError(
                f"mcp[{self._server.name}] {method}: SSE stream ended "
                f"without a response to id={req_id}"
            )
        return our_response

    async def _post_notification(self, method: str, params: dict) -> None:
        assert self._http is not None
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._post_raw_payload(payload, descriptor=method)

    async def _post_raw_payload(
        self, payload: dict, *, descriptor: str = "raw",
    ) -> None:
        """POST an already-assembled JSON-RPC payload (response / notification).

        Unlike ``_post_notification`` this does not wrap the payload;
        used to send responses to server-initiated requests like
        ``sampling/createMessage``. Failures are logged at debug and
        never raised — the ongoing response-to-our-POST stream takes
        priority.
        """
        assert self._http is not None
        try:
            await self._http.post(
                self._server.url,
                content=json.dumps(payload).encode("utf-8"),
                headers=self._base_headers(with_session=True),
                timeout=self._server.timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            log.debug(
                "mcp[%s] %s post failed: %s",
                self._server.name, descriptor, e,
            )


def _parse_sse_events(body: bytes) -> list[bytes]:
    """Return every ``data:`` payload in *body*, one per SSE event.

    The server may interleave server-to-client JSON-RPC requests
    (e.g. ``sampling/createMessage``) before the final response.
    Every event's data lines are concatenated into one bytes blob;
    events without any ``data:`` line are skipped.
    """
    text = body.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[bytes] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            out.append("".join(data_lines).encode("utf-8"))
    return out


def _parse_sse_final_message(body: bytes) -> bytes:
    """Return the last SSE event's data payload (back-compat shim)."""
    events = _parse_sse_events(body)
    return events[-1] if events else b""


def _is_server_request(frame: dict) -> bool:
    """Classify a JSON-RPC frame as a server-to-client *request*.

    A frame is a server request when it has a ``method`` AND an
    ``id`` AND neither ``result`` nor ``error``. Notifications
    (no ``id``) and responses-to-our-POST (``result``/``error``
    present) are not server requests for dispatch purposes.
    """
    if not isinstance(frame, dict):
        return False
    if "method" not in frame:
        return False
    if "id" not in frame:
        return False
    if "result" in frame or "error" in frame:
        return False
    return True
