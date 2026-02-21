"""LLM client — OpenAI-compatible HTTP calls."""

from __future__ import annotations

import contextvars
import json
import os
import time

import httpx

from kiso import audit
from kiso.config import Config, Provider

# Roles that require structured output (response_format with json_schema).
STRUCTURED_ROLES = {"planner", "reviewer", "curator"}


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


def get_provider(config: Config, model_string: str) -> tuple[Provider, str]:
    """Resolve a model string to (provider, model_name).

    "ollama:llama3"       → provider "ollama", model "llama3"
    "minimax/minimax-m2.5" → first provider, model "minimax/minimax-m2.5"
    """
    if ":" in model_string:
        provider_name, model_name = model_string.split(":", 1)
        if provider_name not in config.providers:
            raise LLMError(f"Provider '{provider_name}' not found in config")
        return config.providers[provider_name], model_name
    # No colon → use the first listed provider
    if not config.providers:
        raise LLMError("No providers configured")
    first_name = next(iter(config.providers))
    return config.providers[first_name], model_string


def _get_api_key(provider: Provider) -> str | None:
    """Resolve the API key from environment."""
    if not provider.api_key_env:
        return None
    key = os.environ.get(provider.api_key_env)
    if not key:
        raise LLMError(
            f"API key env var '{provider.api_key_env}' is not set"
        )
    return key


async def call_llm(
    config: Config,
    role: str,
    messages: list[dict],
    response_format: dict | None = None,
    session: str = "",
) -> str:
    """Call an LLM. Returns the response content string.

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
    api_key = _get_api_key(provider)

    if role in STRUCTURED_ROLES and response_format is None:
        raise LLMError(f"Role '{role}' requires structured output but no response_format given")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: dict = {
        "model": model_name,
        "messages": messages,
    }
    if response_format:
        payload["response_format"] = response_format

    url = provider.base_url.rstrip("/") + "/chat/completions"

    # Resolve provider name for audit
    provider_name = model_string.split(":", 1)[0] if ":" in model_string else next(iter(config.providers))

    t0 = time.perf_counter()

    llm_timeout = int(config.settings.get("exec_timeout", 120))
    async with httpx.AsyncClient(timeout=llm_timeout) as client:
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            raise LLMError(f"LLM call timed out ({role}, {model_name})")
        except httpx.RequestError as e:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
            raise LLMError(f"LLM request failed: {e}")

    duration_ms = int((time.perf_counter() - t0) * 1000)

    if resp.status_code != 200:
        audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
        # Try to extract error detail from JSON body (OpenRouter returns JSON errors)
        detail = resp.text[:500] if resp.text else ""
        if not detail:
            try:
                err_data = resp.json()
                detail = json.dumps(err_data, indent=None)[:500]
            except Exception:
                detail = "(empty response body)"
        # Add actionable hints for common HTTP errors
        hint = ""
        if resp.status_code == 401:
            hint = " — check your API key in ~/.kiso/.env"
        elif resp.status_code == 402:
            hint = " — insufficient credits on your API account"
        elif resp.status_code == 400:
            hint = " — model may be unavailable or API key invalid"
        elif resp.status_code == 429:
            hint = " — rate limited, try again shortly"
        raise LLMError(
            f"LLM returned {resp.status_code} for {role} ({model_name}): {detail}{hint}"
        )

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
        raise LLMError(f"Unexpected LLM response format: {e}")

    if not content:
        audit.log_llm_call(session, role, model_name, provider_name, 0, 0, duration_ms, "error")
        raise LLMError(f"Empty response from LLM ({role}, {model_name})")

    # Extract token usage
    usage = data.get("usage") or {}
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    audit.log_llm_call(session, role, model_name, provider_name, input_tokens, output_tokens, duration_ms, "ok")

    # Accumulate usage for per-message tracking
    entries = _llm_usage_entries.get(None)
    if entries is not None:
        entries.append({
            "role": role,
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

    return content
