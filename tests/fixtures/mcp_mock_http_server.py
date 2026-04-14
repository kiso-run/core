"""In-process Streamable HTTP MCP mock server.

Built on FastAPI so it runs entirely inside the test process via
``httpx.ASGITransport``. No sockets, no port binding, no teardown
race. Each test creates a fresh app with a chosen scenario.

Scenarios are parameterised at app-construction time via
``make_app(scenario=...)``. Supported scenarios:

- ``happy_json``: every POST returns a single ``application/json``
  response. Session-id generated on initialize, required on
  subsequent requests.
- ``happy_sse``: POSTs that would return a response instead open an
  SSE stream ending with the JSON-RPC response as the final event.
- ``session_expires``: returns 404 on the second tool call, forcing
  a client re-initialize.
- ``legacy_405``: responds 405 to every POST, simulating a server
  that only speaks the deprecated HTTP+SSE transport.
- ``protocol_version_mismatch``: returns an error on initialize
  with an unsupported protocol version.
- ``stateless``: initialize response omits the ``Mcp-Session-Id``
  header (server is stateless per MCP spec); subsequent requests
  must succeed without a session id header.

The app is minimal and deliberately not a full MCP spec
implementation; it only covers the shapes the tests need.
"""

from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse


def _initialize_result(req_id) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "mock-http-mcp",
                "title": "Mock HTTP MCP",
                "version": "0.1.0",
            },
        },
    }


def _tools_list_result(req_id) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "tools": [
                {
                    "name": "ping",
                    "description": "returns pong",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ],
        },
    }


def _tools_call_result(req_id, args: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [
                {"type": "text", "text": f"pong:{args.get('echo', '')}"}
            ],
        },
    }


def _error_response(req_id, code: int, message: str, data: dict | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def make_app(scenario: str = "happy_json") -> FastAPI:
    app = FastAPI()

    # Per-app state. Not threadsafe but each test creates a new app.
    state = {
        "session_id": None,
        "call_count": 0,
    }

    @app.post("/mcp")
    async def mcp_post(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="mcp-session-id"),
        mcp_protocol_version: str | None = Header(
            default=None, alias="mcp-protocol-version"
        ),
        accept: str = Header(default=""),
    ):
        if scenario == "legacy_405":
            return Response(status_code=405, content=b"legacy transport only")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")

        method = body.get("method")
        req_id = body.get("id")

        if method == "initialize":
            if scenario == "protocol_version_mismatch":
                return JSONResponse(
                    _error_response(
                        req_id,
                        -32602,
                        "Unsupported protocol version",
                        {"supported": ["1999-01-01"]},
                    )
                )
            session_id = str(uuid.uuid4())
            state["session_id"] = session_id
            state["call_count"] = 0  # reset per-session counters on re-init
            response_body = _initialize_result(req_id)
            if scenario == "stateless":
                # Stateless server: no session id issued. Mirror the
                # real-world behaviour of hosted MCP endpoints that do
                # not need per-client state (e.g. Google Maps Grounding
                # Lite). Subsequent requests must not be rejected for
                # missing a session header.
                state["session_id"] = None
                headers: dict[str, str] = {}
            else:
                headers = {"Mcp-Session-Id": session_id}

            if scenario == "happy_sse":
                async def _stream() -> AsyncIterator[bytes]:
                    yield b"event: message\ndata: " + json.dumps(response_body).encode() + b"\n\n"
                return StreamingResponse(
                    _stream(),
                    media_type="text/event-stream",
                    headers=headers,
                )
            return JSONResponse(response_body, headers=headers)

        if method == "notifications/initialized":
            return Response(status_code=202)

        # All other RPCs require a session id matching, unless the
        # server is running in stateless mode.
        if scenario != "stateless":
            if mcp_session_id is None or mcp_session_id != state["session_id"]:
                return Response(status_code=404, content=b"session not found")
        else:
            # Stateless mode: a client that (incorrectly) sends a
            # session header is ignored but not rejected.
            pass

        if method == "tools/list":
            if scenario == "happy_sse":
                async def _stream() -> AsyncIterator[bytes]:
                    yield b"event: message\ndata: " + json.dumps(
                        _tools_list_result(req_id)
                    ).encode() + b"\n\n"
                return StreamingResponse(_stream(), media_type="text/event-stream")
            return JSONResponse(_tools_list_result(req_id))

        if method == "tools/call":
            state["call_count"] += 1
            if scenario == "session_expires" and state["call_count"] >= 2:
                state["session_id"] = None
                return Response(status_code=404, content=b"session expired")
            args = (body.get("params") or {}).get("arguments") or {}
            return JSONResponse(_tools_call_result(req_id, args))

        if method == "notifications/cancelled":
            return Response(status_code=202)

        return JSONResponse(_error_response(req_id, -32601, f"unknown: {method}"))

    @app.delete("/mcp")
    async def mcp_delete(
        mcp_session_id: str | None = Header(default=None, alias="mcp-session-id"),
    ):
        if mcp_session_id and mcp_session_id == state["session_id"]:
            state["session_id"] = None
            return Response(status_code=204)
        return Response(status_code=404)

    return app
