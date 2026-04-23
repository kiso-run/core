"""Handler for server-initiated ``sampling/createMessage`` requests.

The MCP spec allows an MCP server to ask the client to produce an
LLM completion on its behalf via the ``sampling/createMessage``
bidirectional JSON-RPC request. The typical use case is a server
that wants to add an LLM-powered step to one of its tools without
carrying provider credentials — Kiso's client already has those,
so the server delegates.

This module owns the policy and rendering:

- ``mcp_sampling_enabled`` (config setting, default ``True``) —
  when ``False``, every request returns the standard JSON-RPC
  ``method not supported`` error so the server can gracefully
  fall back.
- ``SAMPLING_MAX_TOKENS_CEILING`` — hard upper bound applied to
  whatever ``maxTokens`` the server asks for. Sampling requests
  flow the same per-message LLM budget as any other call.
- The ``mcp_sampling`` role in :mod:`kiso.config` picks the model.

The transport layer (stdio / http) handles the actual JSON-RPC
framing. This module is transport-agnostic: it takes the parsed
request dict and returns a parsed response dict.
"""

from __future__ import annotations

import logging
from typing import Any

from kiso.config import Config, setting_bool
from kiso.llm import LLMBudgetExceeded, LLMError, call_llm

log = logging.getLogger(__name__)

SAMPLING_ROLE = "mcp_sampling"
SAMPLING_METHOD = "sampling/createMessage"
SAMPLING_MAX_TOKENS_CEILING = 4096

# JSON-RPC error codes per spec.
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


def _build_messages_from_params(params: dict) -> list[dict]:
    """Translate MCP ``sampling/createMessage`` params into OpenAI-format
    chat messages suitable for :func:`kiso.llm.call_llm`.

    ``systemPrompt`` becomes a leading system message. Each MCP
    message's ``content`` is flattened: a single text block yields
    the raw string; a list of content blocks yields their text
    concatenated by newlines; non-text blocks degrade to typed
    placeholders so the model at least knows they existed.
    """
    messages: list[dict] = []
    system_prompt = params.get("systemPrompt")
    if isinstance(system_prompt, str) and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    for raw in params.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user")
        content = _flatten_content(raw.get("content"))
        messages.append({"role": role, "content": content})
    return messages


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text":
            parts.append(str(item.get("text", "")))
        elif itype in ("image", "audio"):
            mime = item.get("mimeType", f"{itype}/?")
            parts.append(f"[{itype}: {mime}]")
        else:
            parts.append(f"[content type {itype!r}]")
    return "\n".join(parts)


def _clamp_max_tokens(requested: Any) -> int:
    if isinstance(requested, int) and requested > 0:
        return min(requested, SAMPLING_MAX_TOKENS_CEILING)
    return SAMPLING_MAX_TOKENS_CEILING


def _error_response(req_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _success_response(req_id: Any, text: str, model: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "role": "assistant",
            "content": {"type": "text", "text": text},
            "model": model,
            "stopReason": "endTurn",
        },
    }


async def handle_sampling_request(config: Config, req: dict) -> dict:
    """Build a JSON-RPC response to a ``sampling/createMessage`` request.

    Returns the full response dict (including ``id``) ready to be
    framed by the transport layer. Never raises — both disabled-via-
    policy and LLM failures come back as standard JSON-RPC errors so
    the caller's dispatch loop can forward them verbatim.
    """
    req_id = req.get("id")
    if not setting_bool(config.settings, "mcp_sampling_enabled", default=True):
        return _error_response(
            req_id,
            _METHOD_NOT_FOUND,
            "sampling/createMessage not supported: "
            "mcp_sampling_enabled is disabled on this client",
        )

    params = req.get("params") or {}
    if not isinstance(params, dict):
        return _error_response(
            req_id, _INTERNAL_ERROR, "sampling: params must be an object"
        )

    messages = _build_messages_from_params(params)
    if not messages or all(m.get("role") == "system" for m in messages):
        return _error_response(
            req_id, _INTERNAL_ERROR,
            "sampling: at least one non-system message is required",
        )

    max_tokens = _clamp_max_tokens(params.get("maxTokens"))
    model = config.models.get(SAMPLING_ROLE) or ""

    try:
        text = await call_llm(
            config=config,
            role=SAMPLING_ROLE,
            messages=messages,
            max_tokens=max_tokens,
        )
    except LLMBudgetExceeded as exc:
        log.warning("mcp sampling: per-message LLM budget exceeded: %s", exc)
        return _error_response(
            req_id, _INTERNAL_ERROR,
            f"sampling: LLM call budget exhausted: {exc}",
        )
    except LLMError as exc:
        log.warning("mcp sampling: LLM call failed: %s", exc)
        return _error_response(
            req_id, _INTERNAL_ERROR, f"sampling: LLM call failed: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — any handler failure must reach the server
        log.exception("mcp sampling: unexpected failure")
        return _error_response(
            req_id, _INTERNAL_ERROR, f"sampling: unexpected failure: {exc}",
        )

    return _success_response(req_id, text, model)
