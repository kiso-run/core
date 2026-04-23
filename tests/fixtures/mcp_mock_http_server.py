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
- ``resources_happy``: declares the ``resources`` capability on
  initialize and exposes two resources via ``resources/list``; text
  body returned by ``resources/read``.
- ``resources_error``: ``resources/read`` returns a JSON-RPC error.
- ``prompts_happy``: declares the ``prompts`` capability and
  exposes one prompt ``greet(name)`` via ``prompts/list``; a
  successful ``prompts/get`` renders a single user message.
- ``prompts_error``: ``prompts/get`` returns a JSON-RPC error.

The app is minimal and deliberately not a full MCP spec
implementation; it only covers the shapes the tests need.
"""

from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse


def _initialize_result(req_id, scenario: str = "") -> dict:
    caps: dict = {"tools": {"listChanged": False}}
    if scenario.startswith("resources_"):
        caps["resources"] = {"listChanged": False}
    if scenario.startswith("prompts_"):
        caps["prompts"] = {"listChanged": False}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": caps,
            "serverInfo": {
                "name": "mock-http-mcp",
                "title": "Mock HTTP MCP",
                "version": "0.1.0",
            },
        },
    }


def _resources_list_result(req_id) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "resources": [
                {
                    "uri": "kiso://http/log",
                    "name": "http-log",
                    "description": "HTTP transport log",
                    "mimeType": "text/plain",
                },
                {
                    "uri": "kiso://http/row/7",
                    "name": "row-7",
                    "description": "HTTP row 7",
                    "mimeType": "application/json",
                },
            ],
        },
    }


def _prompts_list_result(req_id) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "prompts": [
                {
                    "name": "greet",
                    "description": "Greet a person",
                    "arguments": [
                        {"name": "name", "description": "who to greet",
                         "required": True},
                    ],
                },
            ],
        },
    }


def _prompts_get_result(req_id, name: str, args: dict) -> dict:
    who = args.get("name", "world")
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "description": f"rendered:{name}",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": f"Hello {who}!"},
                },
            ],
        },
    }


def _resources_read_result(req_id, uri: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": f"http-body-of:{uri}",
                },
            ],
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
            response_body = _initialize_result(req_id, scenario)
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

        if method == "resources/list":
            if scenario.startswith("resources_"):
                return JSONResponse(_resources_list_result(req_id))
            return JSONResponse(
                _error_response(req_id, -32601, "resources not supported")
            )

        if method == "resources/read":
            uri = (body.get("params") or {}).get("uri", "")
            if scenario == "resources_error":
                return JSONResponse(
                    _error_response(req_id, -32002, f"cannot read {uri}")
                )
            if scenario.startswith("resources_"):
                return JSONResponse(_resources_read_result(req_id, uri))
            return JSONResponse(
                _error_response(req_id, -32601, "resources not supported")
            )

        if method == "prompts/list":
            if scenario.startswith("prompts_"):
                return JSONResponse(_prompts_list_result(req_id))
            return JSONResponse(
                _error_response(req_id, -32601, "prompts not supported")
            )

        if method == "prompts/get":
            params = body.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if scenario == "prompts_error":
                return JSONResponse(
                    _error_response(req_id, -32602, f"cannot render {name}")
                )
            if scenario.startswith("prompts_"):
                return JSONResponse(_prompts_get_result(req_id, name, args))
            return JSONResponse(
                _error_response(req_id, -32601, "prompts not supported")
            )

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
