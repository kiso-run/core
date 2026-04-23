"""Standalone minimal stdio MCP server used by the MCP client tests.

Speaks real newline-delimited JSON-RPC on stdin/stdout. Accepts a
scenario name via ``MOCK_MCP_SCENARIO`` env var to drive deterministic
fault injection from test code. Every scenario handles the full
lifecycle (initialize, initialized notification, optional tools/list,
optional tools/call, shutdown on stdin EOF).

Scenarios:

- ``happy`` (default): normal server with two methods ``echo`` and
  ``add``. ``echo`` returns the ``text`` arg as a text content block.
  ``add`` returns the sum of ``a`` and ``b`` as text.
- ``slow_init``: delays the initialize response by
  ``MOCK_MCP_INIT_DELAY_S`` seconds (default 3).
- ``crash_on_call``: exits with status 1 on the first ``tools/call``.
- ``large_tools_list``: returns 25 methods split across 3 pages via
  the ``nextCursor`` mechanism (page_size=10).
- ``mixed_content``: ``tools/call`` returns text + image (1x1 PNG
  base64) + structuredContent.
- ``is_error``: ``tools/call`` returns ``isError: true`` with an error
  text content block.
- ``stderr_flood``: writes 256KB to stderr before responding to
  initialize, exercising the non-blocking stderr reader.
- ``bad_frame``: writes a non-JSON line to stdout before a valid
  response to test framing recovery.
- ``no_exit``: ignores stdin EOF, simulating a server that does not
  shut down on close. Used by the SIGTERM escalation test.
- ``swallow_sigterm``: ignores SIGTERM, forcing the client to fall
  through to SIGKILL.
- ``resources_happy``: same lifecycle as ``happy``, plus two
  resources exposed via ``resources/list`` (``kiso://logs/today`` and
  ``kiso://db/row/42``) and a working ``resources/read`` returning
  a text body.
- ``resources_pagination``: ``resources/list`` returns 25 resources
  split across 3 pages via ``nextCursor`` (page_size=10).
- ``resources_binary``: ``resources/read`` returns a base64-encoded
  binary blob (1x1 PNG) for ``kiso://img/logo``.
- ``resources_error``: ``resources/read`` returns a JSON-RPC error
  for any URI.
- ``prompts_happy``: same lifecycle as ``happy``, plus two prompts
  (``code_review(repo, focus?)`` and ``translate(text, lang)``)
  exposed via ``prompts/list`` and a working ``prompts/get``
  returning a single user message with rendered text.
- ``prompts_pagination``: ``prompts/list`` returns 25 prompts
  paginated across 3 pages (page_size=10).
- ``prompts_error``: ``prompts/get`` returns a JSON-RPC error for
  any prompt name.

Each JSON-RPC message on stdin is a single line of JSON terminated
by ``\\n``. No embedded newlines, per MCP spec.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
import time

PROTOCOL_VERSION = "2025-06-18"

SCENARIO = os.environ.get("MOCK_MCP_SCENARIO", "happy")
INIT_DELAY_S = float(os.environ.get("MOCK_MCP_INIT_DELAY_S", "3"))


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _initialize_result(req_id) -> dict:
    caps: dict = {"tools": {"listChanged": False}}
    if SCENARIO.startswith("resources_"):
        caps["resources"] = {"listChanged": False}
    if SCENARIO.startswith("prompts_"):
        caps["prompts"] = {"listChanged": False}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": caps,
            "serverInfo": {
                "name": "mock-mcp",
                "title": "Mock MCP Server",
                "version": "0.1.0",
            },
            "instructions": f"scenario={SCENARIO}",
        },
    }


def _resources_list_happy() -> list[dict]:
    return [
        {
            "uri": "kiso://logs/today",
            "name": "today-log",
            "description": "Today's application log",
            "mimeType": "text/plain",
        },
        {
            "uri": "kiso://db/row/42",
            "name": "row-42",
            "description": "Row 42 from the primary table",
            "mimeType": "application/json",
        },
    ]


def _resources_list_large_page(cursor):
    total = 25
    page_size = 10
    start = 0 if cursor is None else int(cursor)
    end = min(start + page_size, total)
    page = [
        {
            "uri": f"kiso://gen/{i}",
            "name": f"gen-{i}",
            "description": f"auto-generated resource {i}",
            "mimeType": "text/plain",
        }
        for i in range(start, end)
    ]
    next_cursor = str(end) if end < total else None
    return page, next_cursor


def _prompts_list_happy() -> list[dict]:
    return [
        {
            "name": "code_review",
            "description": "Review a repository for a given focus",
            "arguments": [
                {
                    "name": "repo",
                    "description": "repository path",
                    "required": True,
                },
                {
                    "name": "focus",
                    "description": "what to focus on",
                    "required": False,
                },
            ],
        },
        {
            "name": "translate",
            "description": "Translate a text snippet",
            "arguments": [
                {"name": "text", "description": "text to translate",
                 "required": True},
                {"name": "lang", "description": "target language",
                 "required": True},
            ],
        },
    ]


def _prompts_list_large_page(cursor):
    total = 25
    page_size = 10
    start = 0 if cursor is None else int(cursor)
    end = min(start + page_size, total)
    page = [
        {
            "name": f"prompt_{i}",
            "description": f"auto-generated prompt {i}",
            "arguments": [],
        }
        for i in range(start, end)
    ]
    next_cursor = str(end) if end < total else None
    return page, next_cursor


def _prompts_get_happy(name: str, args: dict) -> dict:
    if name == "code_review":
        repo = args.get("repo", "")
        focus = args.get("focus", "overall quality")
        text = f"Review {repo} focusing on {focus}."
    elif name == "translate":
        snippet = args.get("text", "")
        lang = args.get("lang", "")
        text = f"Translate '{snippet}' to {lang}."
    else:
        text = f"Unknown prompt {name}"
    return {
        "description": f"rendered:{name}",
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": text},
            },
        ],
    }


_BINARY_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db400000000"
        "49454e44ae426082"
    )
).decode()


def _error_response(req_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _tools_list_happy() -> list[dict]:
    return [
        {
            "name": "echo",
            "title": "Echo",
            "description": "Echo the text argument back as a text content block",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "add",
            "title": "Add",
            "description": "Add two numbers and return the sum",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        },
    ]


def _tools_list_large_page(cursor: str | None) -> tuple[list[dict], str | None]:
    total = 25
    page_size = 10
    start = 0 if cursor is None else int(cursor)
    end = min(start + page_size, total)
    page = [
        {
            "name": f"m{i}",
            "title": f"Method {i}",
            "description": f"auto-generated method {i}",
            "inputSchema": {"type": "object", "properties": {}},
        }
        for i in range(start, end)
    ]
    next_cursor = str(end) if end < total else None
    return page, next_cursor


def _handle(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        if SCENARIO == "slow_init":
            time.sleep(INIT_DELAY_S)
        if SCENARIO == "stderr_flood":
            sys.stderr.write("x" * (256 * 1024))
            sys.stderr.write("\n")
            sys.stderr.flush()
        if SCENARIO == "bad_frame":
            sys.stdout.write("this is not valid json\n")
            sys.stdout.flush()
        return _initialize_result(req_id)

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        if SCENARIO == "large_tools_list":
            cursor = params.get("cursor")
            page, next_cursor = _tools_list_large_page(cursor)
            result = {"tools": page}
            if next_cursor is not None:
                result["nextCursor"] = next_cursor
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _tools_list_happy()},
        }

    if method == "tools/call":
        if SCENARIO == "crash_on_call":
            sys.exit(1)
        name = params.get("name")
        args = params.get("arguments") or {}
        if SCENARIO == "is_error":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "simulated failure"}],
                    "isError": True,
                },
            }
        if SCENARIO == "mixed_content":
            tiny_png = base64.b64encode(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                    "890000000d49444154789c6300010000000500010d0a2db400000000"
                    "49454e44ae426082"
                )
            ).decode()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": "rendered"},
                        {
                            "type": "image",
                            "data": tiny_png,
                            "mimeType": "image/png",
                        },
                    ],
                    "structuredContent": {"ok": True, "value": 42},
                },
            }
        # happy path
        if name == "echo":
            text = args.get("text", "")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }
        if name == "add":
            total = (args.get("a") or 0) + (args.get("b") or 0)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": str(total)}],
                },
            }
        return _error_response(req_id, -32601, f"unknown method: {name}")

    if method == "notifications/cancelled":
        return None

    if method == "resources/list":
        if SCENARIO == "resources_pagination":
            cursor = params.get("cursor")
            page, next_cursor = _resources_list_large_page(cursor)
            result = {"resources": page}
            if next_cursor is not None:
                result["nextCursor"] = next_cursor
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        if SCENARIO.startswith("resources_"):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"resources": _resources_list_happy()},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resources": []},
        }

    if method == "resources/read":
        uri = params.get("uri", "")
        if SCENARIO == "resources_error":
            return _error_response(req_id, -32002, f"cannot read {uri}")
        if SCENARIO == "resources_binary":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "image/png",
                            "blob": _BINARY_PNG,
                        },
                    ],
                },
            }
        if SCENARIO.startswith("resources_"):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "text/plain",
                            "text": f"body-of:{uri}",
                        },
                    ],
                },
            }
        return _error_response(req_id, -32601, "resources not exposed")

    if method == "prompts/list":
        if SCENARIO == "prompts_pagination":
            cursor = params.get("cursor")
            page, next_cursor = _prompts_list_large_page(cursor)
            result = {"prompts": page}
            if next_cursor is not None:
                result["nextCursor"] = next_cursor
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        if SCENARIO.startswith("prompts_"):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"prompts": _prompts_list_happy()},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"prompts": []},
        }

    if method == "prompts/get":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if SCENARIO == "prompts_error":
            return _error_response(
                req_id, -32602, f"cannot render prompt {name}"
            )
        if SCENARIO.startswith("prompts_"):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": _prompts_get_happy(name, args),
            }
        return _error_response(req_id, -32601, "prompts not exposed")

    if req_id is not None:
        return _error_response(req_id, -32601, f"unknown RPC method: {method}")
    return None


def main() -> int:
    if SCENARIO == "swallow_sigterm":
        signal.signal(signal.SIGTERM, lambda *_: None)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                sys.stderr.write(f"mock: bad json: {line}\n")
                continue
            response = _handle(req)
            if response is not None:
                _emit(response)
    except KeyboardInterrupt:
        return 0

    if SCENARIO == "no_exit":
        while True:
            time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
