"""LLM client — OpenAI-compatible HTTP calls."""

from __future__ import annotations

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


def get_provider(config: Config, model_string: str) -> tuple[Provider, str]:
    """Resolve a model string to (provider, model_name).

    "ollama:llama3"       → provider "ollama", model "llama3"
    "moonshotai/kimi-k2.5" → first provider, model "moonshotai/kimi-k2.5"
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
        raise LLMError(
            f"LLM returned {resp.status_code}: {resp.text[:500]}"
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

    return content
