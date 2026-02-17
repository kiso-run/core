"""LLM client — OpenAI-compatible HTTP calls."""

from __future__ import annotations

import json
import os

import httpx

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

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            raise LLMError(f"LLM call timed out ({role}, {model_name})")
        except httpx.RequestError as e:
            raise LLMError(f"LLM request failed: {e}")

    if resp.status_code != 200:
        raise LLMError(
            f"LLM returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError(f"Unexpected LLM response format: {e}")

    return content
