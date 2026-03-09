"""LLM client — OpenAI-compatible HTTP calls with SSE streaming."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time

import httpx

from kiso import audit
from kiso.config import Config, LLM_API_KEY_ENV, MAX_TOKENS_DEFAULTS, Provider, REASONING_DEFAULTS
from kiso.text import extract_thinking

log = logging.getLogger(__name__)

# Roles that require structured output (response_format with json_schema).
STRUCTURED_ROLES = {"planner", "reviewer", "curator", "briefer"}

# response_format type values (OpenAI API).
_FMT_JSON_SCHEMA = "json_schema"
_FMT_JSON_OBJECT = "json_object"

# Models that rejected json_schema and need json_object instead.
# Cached per model string to avoid retrying the json_schema format on every call.
_json_object_only_models: set[str] = set()

# Shared long-lived HTTP client, initialized by main.py lifespan.
# When set, call_llm reuses the connection pool instead of opening a new
# TCP/TLS connection per call. Falls back to a per-call client when None.
_http_client: httpx.AsyncClient | None = None


async def init_http_client(timeout: float) -> None:
    """Create the shared HTTP client. Called at server startup.

    If a client already exists (e.g. re-initialisation), it is closed first
    to avoid leaking the underlying TCP connection pool.
    """
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = httpx.AsyncClient(timeout=timeout)


async def close_http_client() -> None:
    """Close the shared HTTP client. Called at server shutdown."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


class LLMError(Exception):
    """Any LLM call failure."""


class LLMBudgetExceeded(LLMError):
    """Raised when per-message LLM call budget is exhausted."""


# Per-message LLM call budget tracking via contextvars.
_llm_budget_max: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_llm_budget_max", default=None,
)
_llm_budget_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_llm_budget_count", default=0,
)


# Per-message token usage tracking via contextvars.
_llm_usage_entries: contextvars.ContextVar[list] = contextvars.ContextVar(
    "_llm_usage_entries", default=None,
)


def reset_usage_tracking() -> None:
    """Start a new usage accumulator for the current message."""
    _llm_usage_entries.set([])


def get_usage_summary() -> dict:
    """Return aggregated token usage from the current accumulator.

    Returns dict with keys: input_tokens, output_tokens, model (last seen).
    """
    entries = _llm_usage_entries.get(None) or []
    total_in = sum(e["input_tokens"] for e in entries)
    total_out = sum(e["output_tokens"] for e in entries)
    model = entries[-1]["model"] if entries else None
    return {"input_tokens": total_in, "output_tokens": total_out, "model": model}


def get_usage_index() -> int:
    """Return current entry count (use as start_index for get_usage_since)."""
    entries = _llm_usage_entries.get(None) or []
    return len(entries)


def get_usage_since(start_index: int) -> dict:
    """Get usage accumulated since *start_index*.

    Returns dict with keys: input_tokens, output_tokens, model, calls.
    ``calls`` is the raw list of per-LLM-call entries (role, model, tokens).
    """
    entries = _llm_usage_entries.get(None) or []
    subset = entries[start_index:]
    return {
        "input_tokens": sum(e["input_tokens"] for e in subset),
        "output_tokens": sum(e["output_tokens"] for e in subset),
        "model": subset[-1]["model"] if subset else None,
        "calls": [dict(e) for e in subset],
    }


def set_llm_budget(max_calls: int) -> None:
    """Set per-message LLM call budget. Resets the counter to 0."""
    _llm_budget_max.set(max_calls)
    _llm_budget_count.set(0)


def clear_llm_budget() -> None:
    """Clear the LLM budget (disable tracking)."""
    _llm_budget_max.set(None)
    _llm_budget_count.set(0)


def get_llm_call_count() -> int:
    """Return the current LLM call count for the active budget."""
    return _llm_budget_count.get()


# Per-session inflight LLM call tracking.
# Populated just before the HTTP request, cleared in the finally block.
_inflight_calls: dict[str, dict] = {}


def get_inflight_call(session: str) -> dict | None:
    """Return the inflight LLM call for *session*, or None."""
    return _inflight_calls.get(session)


def get_provider(config: Config, model_string: str) -> tuple[Provider, str]:
    """Resolve a model string to (provider, model_name).

    "ollama:llama3"                       → provider "ollama", model "llama3"
    "deepseek/deepseek-v3.2"              → first provider, model "deepseek/deepseek-v3.2"
    "google/gemini-2.5-flash-lite:online" → first provider, full string as model
                                            (colon ignored when left side isn't a known provider)
    """
    if ":" in model_string:
        provider_name, model_name = model_string.split(":", 1)
        if provider_name in config.providers:
            return config.providers[provider_name], model_name
        # Colon is part of the model name (e.g. "google/gemini:online"),
        # not a provider selector — fall through to first-provider logic.
    if not config.providers:
        raise LLMError("No providers configured")
    first_name = next(iter(config.providers))
    return config.providers[first_name], model_string


def _get_api_key() -> str | None:
    """Return the LLM API key from KISO_LLM_API_KEY, or None if unset."""
    return os.environ.get(LLM_API_KEY_ENV)


async def _read_sse_stream(
    response: httpx.Response,
) -> tuple[str, str, int, int]:
    """Read an OpenAI-compatible SSE stream.

    Returns (content, reasoning_content, prompt_tokens, completion_tokens).
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0

    async for raw_line in response.aiter_lines():
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if line == "data: [DONE]":
            break
        if not line.startswith("data: "):
            continue
        try:
            chunk = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            c = delta.get("content")
            if c:
                content_parts.append(c)
            r = delta.get("reasoning_content")
            if r:
                reasoning_parts.append(r)

        usage = chunk.get("usage")
        if usage:
            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage.get("completion_tokens", completion_tokens)

    return "".join(content_parts), "".join(reasoning_parts), prompt_tokens, completion_tokens


async def call_llm(
    config: Config,
    role: str,
    messages: list[dict],
    response_format: dict | None = None,
    session: str = "",
    max_tokens: int | None = None,
) -> str:
    """Call an LLM via streaming SSE. Returns the response content string.

    - role: one of "planner", "reviewer", "curator", "worker", "summarizer"
    - messages: OpenAI-format message list [{"role": ..., "content": ...}]
    - response_format: JSON schema dict for structured output (required for
      planner/reviewer/curator)
    """
    # Budget enforcement
    budget_max = _llm_budget_max.get(None)
    if budget_max is not None:
        count = _llm_budget_count.get(0)
        if count >= budget_max:
            raise LLMBudgetExceeded(
                f"LLM call budget exhausted ({count}/{budget_max} calls used)"
            )
        _llm_budget_count.set(count + 1)

    model_string = config.models.get(role)
    if not model_string:
        raise LLMError(f"No model configured for role '{role}'")

    provider, model_name = get_provider(config, model_string)
    api_key = _get_api_key()

    if role in STRUCTURED_ROLES and response_format is None:
        raise LLMError(f"Role '{role}' requires structured output but no response_format given")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Downgrade json_schema → json_object for models known to not support it.
    effective_format = response_format
    if (
        response_format
        and response_format.get("type") == _FMT_JSON_SCHEMA
        and model_name in _json_object_only_models
    ):
        effective_format = {"type": _FMT_JSON_OBJECT}

    url = provider.base_url.rstrip("/") + "/chat/completions"

    # Resolve provider name for audit
    provider_name = model_string.split(":", 1)[0] if ":" in model_string else next(iter(config.providers))

    # Pick the right timeout for this role.
    if role == "planner":
        llm_timeout = int(config.settings.get("planner_timeout", config.settings["llm_timeout"]))
    elif role == "messenger":
        llm_timeout = int(config.settings.get("messenger_timeout", config.settings["llm_timeout"]))
    else:
        llm_timeout = int(config.settings["llm_timeout"])

    # Stripped message list — computed lazily for inflight tracking and usage logging
    stripped_messages: list[dict] | None = None

    # Timestamp shared between inflight tracking and usage logging so the CLI
    # can correlate inflight input panels with completed call entries.
    call_ts = time.time()

    # Accumulate results across the retry loop (json_schema → json_object fallback).
    _resp_status = 0
    _error_body = ""
    content = ""
    reasoning_api = ""
    input_tokens = 0
    output_tokens = 0

    for _attempt in range(2):
        payload: dict = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if effective_format:
            payload["response_format"] = effective_format
        # M296: apply per-role max_tokens default when not explicitly set.
        effective_max_tokens = max_tokens if max_tokens is not None else MAX_TOKENS_DEFAULTS.get(role)
        if effective_max_tokens is not None:
            payload["max_tokens"] = effective_max_tokens
        # M271: per-role reasoning config (limits thinking tokens for simple roles)
        reasoning = REASONING_DEFAULTS.get(role)
        if reasoning:
            payload["reasoning"] = reasoning

        t0 = time.perf_counter()

        # Track inflight call so the CLI can show it in real-time
        if session:
            if stripped_messages is None:
                stripped_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
            _inflight_calls[session] = {
                "role": role,
                "model": model_name,
                "messages": stripped_messages,
                "ts": call_ts,
            }

        _temp_client: httpx.AsyncClient | None = None
        try:
            if _http_client is not None:
                _use_client = _http_client
            else:
                _temp_client = httpx.AsyncClient(timeout=llm_timeout)
                _use_client = _temp_client

            async with _use_client.stream(
                "POST", url, headers=headers, json=payload, timeout=llm_timeout,
            ) as resp:
                _resp_status = resp.status_code
                if _resp_status != 200:
                    _error_body = (await resp.aread()).decode(errors="replace")
                    # Detect json_schema rejection → retry once with json_object.
                    if (
                        _attempt == 0
                        and _resp_status == 400
                        and effective_format
                        and effective_format.get("type") == _FMT_JSON_SCHEMA
                        and "response_format" in _error_body.lower()
                    ):
                        _json_object_only_models.add(model_name)
                        log.warning(
                            "Model %s does not support json_schema, falling back to json_object",
                            model_name,
                        )
                        effective_format = {"type": _FMT_JSON_OBJECT}
                        continue  # retry with json_object
                    break  # non-retryable error, handle after loop

                # Read SSE stream
                content, reasoning_api, input_tokens, output_tokens = await _read_sse_stream(resp)

            break  # success
        except httpx.TimeoutException:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            raise LLMError(f"LLM call timed out ({role}, {model_name})")
        except httpx.RequestError as e:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            raise LLMError(f"LLM request failed: {e}")
        finally:
            _inflight_calls.pop(session, None)
            if _temp_client is not None:
                await _temp_client.aclose()

    duration_ms = int((time.perf_counter() - t0) * 1000)

    if _resp_status != 200:
        audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
        # Try to extract error detail
        detail = _error_body[:500] if _error_body else ""
        if not detail:
            detail = "(empty response body)"
        # Add actionable hints for common HTTP errors
        hint = ""
        if _resp_status == 401:
            hint = " — check your API key in ~/.kiso/.env"
        elif _resp_status == 402:
            hint = " — insufficient credits on your API account"
        elif _resp_status == 400:
            hint = " — model may be unavailable or API key invalid"
        elif _resp_status == 429:
            hint = " — rate limited, try again shortly"
        raise LLMError(
            f"LLM returned {_resp_status} for {role} ({model_name}): {detail}{hint}"
        )

    if not content:
        audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
        raise LLMError(f"Empty response from LLM ({role}, {model_name})")

    # Extract thinking/reasoning content.
    # 1) API-level field from streaming deltas
    reasoning_field = reasoning_api.strip()
    # 2) <think>/<thinking> tags embedded in content
    tag_thinking, clean_content = extract_thinking(content)
    # Prefer tags (they're the actual response content); fall back to API field.
    thinking = tag_thinking or reasoning_field
    # If tags were found, use the cleaned content as the response.
    if tag_thinking:
        content = clean_content

    audit.log_llm_call(session, role, model_name, provider_name, input_tokens, output_tokens, duration_ms, "ok")

    # Accumulate usage for per-message tracking
    entries = _llm_usage_entries.get(None)
    if entries is not None:
        if stripped_messages is None:
            stripped_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        entries.append({
            "role": role,
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "thinking": thinking,
            "messages": stripped_messages,
            "response": content,
            "ts": call_ts,
        })

    return content
