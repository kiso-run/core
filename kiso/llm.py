"""LLM client — OpenAI-compatible HTTP calls with SSE streaming."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import contextvars
import json
import logging
import os
import ssl
import time

import httpx

from kiso import audit
from kiso.config import Config, CLASSIFIER_MAX_TOKENS, LLM_API_KEY_ENV, Provider, REASONING_DEFAULTS
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

# transport retry settings. Backoff set to 0 in tests.
_TRANSPORT_RETRY_BACKOFF: float = 1.0
_MAX_TRANSPORT_RETRIES = 2

# rate limit (429/529) retry settings
_MAX_RATE_RETRIES = 4
_RATE_INITIAL_BACKOFF: float = 1.0
_RATE_MAX_BACKOFF: float = 60.0

# circuit breaker — protects against provider-wide degradation.
# When consecutive transport failures exceed the threshold, subsequent
# calls fail immediately instead of wasting time on doomed retries.
_CB_FAILURE_THRESHOLD = 5
_CB_COOLDOWN = 30.0
_cb_consecutive_failures = 0
_cb_open_until: float = 0.0  # monotonic timestamp


def _cb_record_failure() -> None:
    """Record a transport failure.  Opens circuit after threshold."""
    global _cb_consecutive_failures, _cb_open_until
    _cb_consecutive_failures += 1
    if _cb_consecutive_failures >= _CB_FAILURE_THRESHOLD:
        _cb_open_until = time.monotonic() + _CB_COOLDOWN
        log.warning(
            "Circuit breaker OPEN — %d consecutive transport failures, "
            "failing fast for %.0fs",
            _cb_consecutive_failures, _CB_COOLDOWN,
        )


def _cb_record_success() -> None:
    """Reset circuit breaker on success."""
    global _cb_consecutive_failures, _cb_open_until
    _cb_consecutive_failures = 0
    _cb_open_until = 0.0


def _cb_is_open() -> bool:
    """Check if circuit is open (should fail fast)."""
    if _cb_open_until <= 0:
        return False
    if time.monotonic() >= _cb_open_until:
        # Cooldown expired → half-open, allow one probe
        return False
    return True


def _cb_reset() -> None:
    """Reset circuit breaker state (for tests)."""
    global _cb_consecutive_failures, _cb_open_until
    _cb_consecutive_failures = 0
    _cb_open_until = 0.0

# Actionable hints for common HTTP error status codes from LLM providers.
_LLM_ERROR_HINTS: dict[int, str] = {
    400: " — model may be unavailable or API key invalid",
    401: " — check your API key in ~/.kiso/.env",
    402: " — insufficient credits on your API account",
    429: " — rate limited, try again shortly",
}


def _ms_since(t0: float) -> int:
    """Return elapsed milliseconds since *t0* (a perf_counter timestamp)."""
    return int((time.perf_counter() - t0) * 1000)


def _strip_messages(messages: list[dict]) -> list[dict]:
    """Return messages with only role and content keys (strip tool_calls etc.)."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


@asynccontextmanager
async def _http_client_ctx(timeout: float):
    """Yield an httpx.AsyncClient, reusing the shared one or creating a temporary one."""
    if _http_client is not None:
        yield _http_client
    else:
        temp = httpx.AsyncClient(timeout=timeout)
        try:
            yield temp
        finally:
            await temp.aclose()


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


class LLMStallError(LLMError):
    """Raised when the LLM stream stalls (no chunks for stall_timeout seconds)."""


async def _read_sse_stream(
    response: httpx.Response,
    stall_timeout: float = 60,
    inflight_dict: dict | None = None,
) -> tuple[str, str, int, int, str]:
    """Read an OpenAI-compatible SSE stream with stall detection.

    If no line arrives within *stall_timeout* seconds, raises LLMStallError.
    When *inflight_dict* is provided, updates its ``partial_content`` key on
    each content chunk so the CLI can display live streaming output.

    Returns (content, reasoning_content, prompt_tokens, completion_tokens, finish_reason).
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    last_finish_reason = ""

    line_iter = response.aiter_lines().__aiter__()
    while True:
        try:
            raw_line = await asyncio.wait_for(line_iter.__anext__(), timeout=stall_timeout)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            raise LLMStallError(
                f"LLM stream stalled (no data for {stall_timeout}s)"
            )

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
                if inflight_dict is not None:
                    inflight_dict["partial_content"] = "".join(content_parts)
            r = delta.get("reasoning_content")
            if r:
                reasoning_parts.append(r)
            fr = choice.get("finish_reason")
            if fr:
                last_finish_reason = fr

        usage = chunk.get("usage")
        if usage:
            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage.get("completion_tokens", completion_tokens)

    return "".join(content_parts), "".join(reasoning_parts), prompt_tokens, completion_tokens, last_finish_reason


async def call_llm(
    config: Config,
    role: str,
    messages: list[dict],
    response_format: dict | None = None,
    session: str = "",
    max_tokens: int | None = None,
    model_override: str | None = None,
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

    model_string = model_override or config.models.get(role)
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

    llm_timeout = int(config.settings["llm_timeout"])

    stall_timeout = int(config.settings.get("stall_timeout", 60))

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
    _transport_retries = 0
    _rate_retries = 0
    _rate_backoff = _RATE_INITIAL_BACKOFF

    _json_schema_retried = False  # track whether we already fell back to json_object

    # circuit breaker — fail fast when provider is degraded
    if _cb_is_open():
        raise LLMError(
            f"Circuit breaker open — provider transport degraded, failing fast "
            f"[model={model_name}, role={role}]"
        )

    while True:
        payload: dict = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if effective_format:
            payload["response_format"] = effective_format
        # Only set max_tokens for classifier (needs single-word response).
        effective_max_tokens = max_tokens if max_tokens is not None else (
            CLASSIFIER_MAX_TOKENS if role == "classifier" else None
        )
        if effective_max_tokens is not None:
            payload["max_tokens"] = effective_max_tokens
        # per-role reasoning config (limits thinking tokens for simple roles)
        reasoning = REASONING_DEFAULTS.get(role)
        if reasoning:
            payload["reasoning"] = reasoning

        t0 = time.perf_counter()

        # Track inflight call so the CLI can show it in real-time
        if session:
            if stripped_messages is None:
                stripped_messages = _strip_messages(messages)
            _inflight_calls[session] = {
                "role": role,
                "model": model_name,
                "messages": stripped_messages,
                "ts": call_ts,
            }

        try:
            async with _http_client_ctx(llm_timeout) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=payload, timeout=llm_timeout,
                ) as resp:
                    _resp_status = resp.status_code
                    if _resp_status != 200:
                        _error_body = (await resp.aread()).decode(errors="replace")
                        # Detect json_schema rejection → retry once with json_object.
                        if (
                            not _json_schema_retried
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
                            _json_schema_retried = True
                            continue  # retry with json_object
                        # Rate limit (429) or overloaded (529): retry with backoff
                        if _resp_status in (429, 529) and _rate_retries < _MAX_RATE_RETRIES:
                            _rate_retries += 1
                            # Honor Retry-After header if present
                            retry_after_hdr = resp.headers.get("retry-after", "")
                            try:
                                wait = float(retry_after_hdr)
                            except (ValueError, TypeError):
                                wait = _rate_backoff
                            wait = min(wait, _RATE_MAX_BACKOFF)
                            log.warning(
                                "Rate limited (%d), retry %d/%d in %.1fs [model=%s]",
                                _resp_status, _rate_retries, _MAX_RATE_RETRIES,
                                wait, model_name,
                            )
                            await asyncio.sleep(wait)
                            _rate_backoff = min(_rate_backoff * 2, _RATE_MAX_BACKOFF)
                            continue
                        break  # non-retryable error, handle after loop

                    # Read SSE stream — pass inflight dict for live partial output
                    _inflight = _inflight_calls.get(session) if session else None
                    content, reasoning_api, input_tokens, output_tokens, finish_reason = await _read_sse_stream(
                        resp, stall_timeout=stall_timeout, inflight_dict=_inflight,
                    )

            _cb_record_success()
            if finish_reason == "length":
                log.warning(
                    "LLM response truncated (max_tokens hit) [role=%s, model=%s]",
                    role, model_name,
                )
            break  # success
        except LLMStallError:
            duration_ms = _ms_since(t0)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            raise  # already an LLMError subclass — propagate for retry
        except httpx.TimeoutException as e:
            duration_ms = _ms_since(t0)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            raise LLMError(f"LLM call timed out ({type(e).__name__}, {role}, {model_name})")
        except httpx.RequestError as e:
            duration_ms = _ms_since(t0)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            _detail = str(e) or "no detail"
            _cb_record_failure()
            _transport_retries += 1
            if _transport_retries <= _MAX_TRANSPORT_RETRIES:
                backoff = _TRANSPORT_RETRY_BACKOFF * (2 ** (_transport_retries - 1))
                log.warning(
                    "LLM transport retry %d/%d (%s): %s [model=%s] — retrying in %gs",
                    _transport_retries, _MAX_TRANSPORT_RETRIES,
                    type(e).__name__, _detail, model_name, backoff,
                )
                if backoff > 0:
                    await asyncio.sleep(backoff)
                continue  # retry transport
            raise LLMError(f"LLM request failed ({type(e).__name__}): {_detail} [model={model_name}]")
        except ssl.SSLError as e:
            # Mid-stream TLS corruption (BAD_RECORD_MAC, unexpected EOF,
            # etc.) can escape httpx's RequestError wrapping and surface
            # as a bare ssl.SSLError. Treat these as transient transport
            # errors sharing the same bounded retry budget. Certificate
            # verification errors are a permanent config issue and fail
            # fast without retry.
            duration_ms = _ms_since(t0)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            _detail = str(e) or "no detail"
            if isinstance(e, ssl.SSLCertVerificationError):
                raise LLMError(
                    f"LLM request failed ({type(e).__name__}): {_detail} [model={model_name}]"
                )
            _cb_record_failure()
            _transport_retries += 1
            if _transport_retries <= _MAX_TRANSPORT_RETRIES:
                backoff = _TRANSPORT_RETRY_BACKOFF * (2 ** (_transport_retries - 1))
                log.warning(
                    "LLM transport retry %d/%d (%s): %s [model=%s] — retrying in %gs",
                    _transport_retries, _MAX_TRANSPORT_RETRIES,
                    type(e).__name__, _detail, model_name, backoff,
                )
                if backoff > 0:
                    await asyncio.sleep(backoff)
                continue
            raise LLMError(f"LLM request failed ({type(e).__name__}): {_detail} [model={model_name}]")
        finally:
            _inflight_calls.pop(session, None)

    duration_ms = _ms_since(t0)

    if _resp_status != 200:
        audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
        # Try to extract error detail
        detail = _error_body[:500] if _error_body else ""
        if not detail:
            detail = "(empty response body)"
        # Add actionable hints for common HTTP errors
        hint = _LLM_ERROR_HINTS.get(_resp_status, "")
        raise LLMError(
            f"LLM returned {_resp_status} for {role} ({model_name}): {detail}{hint}"
        )

    if not content:
        # reasoning→content fallback for structured roles.
        # Some models put JSON output in reasoning_content instead of content.
        if reasoning_api.strip() and role in STRUCTURED_ROLES and reasoning_api.strip().startswith("{"):
            log.warning(
                "Empty content but reasoning contains JSON (%d chars) for %s/%s — using as fallback",
                len(reasoning_api), role, model_name,
            )
            content = reasoning_api.strip()
        else:
            if reasoning_api.strip():
                log.warning(
                    "Empty content with reasoning (%d chars) for %s/%s — cannot use as fallback",
                    len(reasoning_api), role, model_name,
                )
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
            stripped_messages = _strip_messages(messages)
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
