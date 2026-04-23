#!/usr/bin/env python3
"""Minimal stdio MCP server used as a deterministic test target.

Exposes one tool, ``echo(message)``, and an optional ``persist``
argument that writes the echoed payload into
``$ECHO_WORKSPACE/echo.txt`` so tests can observe per-session
workspace isolation (Kiso strips ``KISO_*`` env vars before spawning
MCP subprocesses, so a non-``KISO_`` name is required).

Implements only the subset of the JSON-RPC / MCP protocol that the Kiso
stdio client exercises: ``initialize``, ``tools/list``, ``tools/call``,
and the ``notifications/initialized`` notification.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROTOCOL_VERSION = "2025-06-18"
TOOLS = [
    {
        "name": "echo",
        "title": "Echo",
        "description": "Echo the given message back to the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "persist": {"type": "boolean"},
            },
            "required": ["message"],
        },
    }
]


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _handle(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != "echo":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool {name!r}"},
            }
        msg = args.get("message", "")
        if args.get("persist"):
            ws = os.environ.get("ECHO_WORKSPACE")
            if ws:
                Path(ws).mkdir(parents=True, exist_ok=True)
                (Path(ws) / "echo.txt").write_text(msg, encoding="utf-8")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": msg}],
                "isError": False,
            },
        }
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = _handle(req)
        if reply is not None:
            _send(reply)


if __name__ == "__main__":
    main()
