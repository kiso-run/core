"""Tests for MCP ``sampling/createMessage`` support.

Business requirement: an MCP server without its own LLM credentials
must be able to ask Kiso — which holds the OpenRouter key and the
model routing table — to produce a completion on its behalf. Kiso's
client advertises the ``sampling`` capability during initialize,
dispatches incoming ``sampling/createMessage`` requests to a
dedicated handler, builds the LLM call through the existing
``call_llm`` infrastructure using the ``mcp_sampling`` role,
clamps ``maxTokens`` to a policy ceiling, counts the call against
``max_llm_calls_per_message``, and refuses with the standard
JSON-RPC error ``method not supported`` when the operator has
disabled sampling via ``mcp_sampling_enabled = false``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiso.config import Config, MODEL_DEFAULTS, SETTINGS_DEFAULTS, Provider
from kiso.llm import LLMBudgetExceeded, _llm_budget_count, _llm_budget_max
from kiso.mcp.config import MCPServer
from kiso.mcp.sampling import (
    SAMPLING_MAX_TOKENS_CEILING,
    _build_messages_from_params,
    handle_sampling_request,
)
from kiso.mcp.stdio import MCPStdioClient
from tests.conftest import full_models, full_settings


def _config(
    *, enabled: bool = True, sampling_model: str | None = None,
) -> Config:
    models = full_models()
    if sampling_model is not None:
        models["mcp_sampling"] = sampling_model
    else:
        models.setdefault("mcp_sampling", "google/gemini-2.5-flash")
    settings = full_settings(mcp_sampling_enabled=enabled)
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://example.com/v1")},
        users={},
        models=models,
        settings=settings,
        raw={},
    )


class TestDefaults:
    def test_mcp_sampling_role_has_default_model(self):
        assert "mcp_sampling" in MODEL_DEFAULTS
        assert MODEL_DEFAULTS["mcp_sampling"]

    def test_mcp_sampling_enabled_default_true(self):
        assert SETTINGS_DEFAULTS["mcp_sampling_enabled"] is True


class TestBuildMessagesFromParams:
    def test_system_prompt_becomes_leading_system_message(self):
        messages = _build_messages_from_params({
            "systemPrompt": "Be concise.",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "hi"}},
            ],
        })
        assert messages[0] == {"role": "system", "content": "Be concise."}
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "hi"

    def test_no_system_prompt_no_system_message(self):
        messages = _build_messages_from_params({
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "hi"}},
            ],
        })
        assert all(m["role"] != "system" for m in messages)

    def test_list_content_text_blocks_concatenated(self):
        messages = _build_messages_from_params({
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                ]},
            ],
        })
        assert "part one" in messages[0]["content"]
        assert "part two" in messages[0]["content"]


class TestHandleSamplingRequest:
    async def test_spec_shaped_response(self):
        config = _config()
        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "sampling/createMessage",
            "params": {
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": "Say hi"}},
                ],
                "maxTokens": 50,
            },
        }
        with patch(
            "kiso.mcp.sampling.call_llm",
            new=AsyncMock(return_value="hello!"),
        ) as mock_call:
            response = await handle_sampling_request(config, req)

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 7
        result = response["result"]
        assert result["role"] == "assistant"
        assert result["content"]["type"] == "text"
        assert result["content"]["text"] == "hello!"
        assert "model" in result
        assert result["stopReason"] == "endTurn"

        # Clamped maxTokens forwarded
        kwargs = mock_call.call_args.kwargs
        assert kwargs["role"] == "mcp_sampling"
        assert kwargs["max_tokens"] == 50

    async def test_maxtokens_clamped_to_ceiling(self):
        config = _config()
        req = {
            "jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage",
            "params": {
                "messages": [{"role": "user", "content": {"type": "text", "text": "x"}}],
                "maxTokens": SAMPLING_MAX_TOKENS_CEILING * 10,
            },
        }
        with patch(
            "kiso.mcp.sampling.call_llm",
            new=AsyncMock(return_value="ok"),
        ) as mock_call:
            await handle_sampling_request(config, req)
        assert mock_call.call_args.kwargs["max_tokens"] == SAMPLING_MAX_TOKENS_CEILING

    async def test_missing_maxtokens_uses_ceiling(self):
        config = _config()
        req = {
            "jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage",
            "params": {
                "messages": [{"role": "user", "content": {"type": "text", "text": "x"}}],
            },
        }
        with patch(
            "kiso.mcp.sampling.call_llm",
            new=AsyncMock(return_value="ok"),
        ) as mock_call:
            await handle_sampling_request(config, req)
        assert mock_call.call_args.kwargs["max_tokens"] == SAMPLING_MAX_TOKENS_CEILING

    async def test_disabled_returns_method_not_supported(self):
        config = _config(enabled=False)
        req = {
            "jsonrpc": "2.0", "id": 99, "method": "sampling/createMessage",
            "params": {
                "messages": [{"role": "user", "content": {"type": "text", "text": "x"}}],
            },
        }
        with patch("kiso.mcp.sampling.call_llm") as mock_call:
            response = await handle_sampling_request(config, req)
        assert response["id"] == 99
        assert "error" in response
        assert response["error"]["code"] == -32601  # method not found
        assert "not supported" in response["error"]["message"].lower()
        mock_call.assert_not_called()

    async def test_llm_error_surfaced_as_internal_error(self):
        from kiso.llm import LLMError

        config = _config()
        req = {
            "jsonrpc": "2.0", "id": 2, "method": "sampling/createMessage",
            "params": {
                "messages": [{"role": "user", "content": {"type": "text", "text": "x"}}],
            },
        }
        with patch(
            "kiso.mcp.sampling.call_llm",
            new=AsyncMock(side_effect=LLMError("boom")),
        ):
            response = await handle_sampling_request(config, req)
        assert response["id"] == 2
        assert "error" in response
        assert response["error"]["code"] == -32603  # internal error

    async def test_counts_against_per_message_budget(self):
        """When the budget context is exhausted, handler returns internal error."""
        config = _config()
        req = {
            "jsonrpc": "2.0", "id": 3, "method": "sampling/createMessage",
            "params": {
                "messages": [{"role": "user", "content": {"type": "text", "text": "x"}}],
            },
        }

        # Prime a zero-sized budget so any call raises LLMBudgetExceeded.
        _llm_budget_max.set(0)
        _llm_budget_count.set(0)
        try:
            # call_llm is NOT patched — we want the real budget enforcement
            # to trigger. Reset after the test.
            response = await handle_sampling_request(config, req)
        finally:
            _llm_budget_max.set(None)
            _llm_budget_count.set(0)

        assert "error" in response
        # Budget-exhausted is surfaced as an internal error.
        assert response["error"]["code"] == -32603


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_mock_stdio_server.py"


def _stdio_server(scenario: str = "sampling_request") -> MCPServer:
    return MCPServer(
        name="mock",
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE)],
        env={"MOCK_MCP_SCENARIO": scenario},
        cwd=None,
        enabled=True,
        timeout_s=10.0,
    )


class TestStdioIntegration:
    async def test_client_advertises_sampling_capability(self):
        """When mcp_sampling_enabled=true, the initialize handshake
        declares the sampling capability to the server."""
        client = MCPStdioClient(
            _stdio_server("happy"),
            config=_config(enabled=True),
        )
        await client.initialize()
        # The client's InitializeRequest must have included
        # capabilities.sampling (verified via the mock server,
        # which echoes client capabilities back in its
        # instructions when scenario='happy').
        # For a leaner assertion, check the client's own state.
        assert client.advertises_sampling is True
        await client.shutdown()

    async def test_client_does_not_advertise_when_disabled(self):
        client = MCPStdioClient(
            _stdio_server("happy"),
            config=_config(enabled=False),
        )
        await client.initialize()
        assert client.advertises_sampling is False
        await client.shutdown()

    async def test_server_initiated_sampling_receives_response(self):
        """A server that emits a sampling/createMessage request before
        responding to tools/call gets a spec-shaped response from the
        client and proceeds to complete the tool call."""
        client = MCPStdioClient(
            _stdio_server("sampling_request"),
            config=_config(enabled=True),
        )
        await client.initialize()
        with patch(
            "kiso.mcp.sampling.call_llm",
            new=AsyncMock(return_value="sampled-ok"),
        ):
            result = await client.call_method(
                "do_sampled_work",
                {},
            )
        # The fixture server returns the client's sampling response text
        # as the tool call's text content, so we can verify end-to-end
        # delivery through the bidirectional channel.
        assert "sampled-ok" in result.stdout_text
        await client.shutdown()

    async def test_server_initiated_sampling_refused_when_disabled(self):
        client = MCPStdioClient(
            _stdio_server("sampling_request"),
            config=_config(enabled=False),
        )
        await client.initialize()
        with patch(
            "kiso.mcp.sampling.call_llm",
            new=AsyncMock(return_value="should-not-be-called"),
        ) as mock_call:
            result = await client.call_method(
                "do_sampled_work",
                {},
            )
        assert "METHOD_NOT_SUPPORTED" in result.stdout_text
        mock_call.assert_not_called()
        await client.shutdown()
