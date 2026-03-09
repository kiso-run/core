"""Tests for kiso/llm.py — LLM client with SSE streaming."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from kiso.config import Config, Provider, SETTINGS_DEFAULTS, MODEL_DEFAULTS
from kiso.llm import (
    LLMBudgetExceeded,
    LLMError,
    _inflight_calls,
    _json_object_only_models,
    call_llm,
    clear_llm_budget,
    close_http_client,
    get_inflight_call,
    get_llm_call_count,
    get_provider,
    get_usage_index,
    init_http_client,
    get_usage_since,
    get_usage_summary,
    reset_usage_tracking,
    set_llm_budget,
    _get_api_key,
)


# --- Minimal config fixtures ---

def _make_config(**overrides) -> Config:
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models={**MODEL_DEFAULTS, "planner": "gpt-4", "worker": "gpt-3.5"},
        settings={**SETTINGS_DEFAULTS},
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


# --- SSE streaming mock helpers ---

class _MockStreamResp:
    """Mock httpx streaming response with SSE lines."""

    def __init__(self, status_code: int, sse_lines: list[str] | None = None, body: str = ""):
        self.status_code = status_code
        self._sse_lines = sse_lines or []
        self._body = body

    async def aiter_lines(self):
        for line in self._sse_lines:
            yield line

    async def aread(self) -> bytes:
        return self._body.encode() if isinstance(self._body, str) else self._body


class _StreamCM:
    """Async context manager wrapping a mock stream response."""

    def __init__(self, response: _MockStreamResp):
        self._resp = response

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


def _ok_stream(
    content: str = "hello",
    usage: dict | None = None,
    reasoning_content: str | None = None,
) -> _StreamCM:
    """Build a mock SSE streaming response for a successful LLM call."""
    lines: list[str] = []
    # Content chunk
    if content:
        delta: dict = {"content": content}
        chunk: dict = {"choices": [{"delta": delta, "index": 0}]}
        lines.append(f"data: {json.dumps(chunk)}")
    # Reasoning chunk (separate from content)
    if reasoning_content:
        r_chunk = {"choices": [{"delta": {"reasoning_content": reasoning_content}, "index": 0}]}
        lines.append(f"data: {json.dumps(r_chunk)}")
    # Final chunk with finish_reason + usage
    final: dict = {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]}
    if usage:
        final["usage"] = usage
    lines.append(f"data: {json.dumps(final)}")
    lines.append("data: [DONE]")
    return _StreamCM(_MockStreamResp(200, lines))


def _error_stream(status_code: int, body: str = "") -> _StreamCM:
    """Build a mock streaming response for an error (non-200 status)."""
    return _StreamCM(_MockStreamResp(status_code, body=body))


def _setup_mock(mock_cls, stream_cm):
    """Set up httpx.AsyncClient mock for streaming. Returns mock_client."""
    mock_client = AsyncMock()
    # stream() must be a regular MagicMock — it returns a sync context manager,
    # not a coroutine.  AsyncMock would wrap it in a coroutine which breaks
    # ``async with client.stream(...)``.
    mock_client.stream = MagicMock(return_value=stream_cm)
    mock_client.aclose = AsyncMock()
    mock_cls.return_value = mock_client
    return mock_client


# --- get_provider ---

class TestGetProvider:
    def test_no_colon_uses_first_provider(self):
        config = _make_config()
        provider, model = get_provider(config, "gpt-4")
        assert model == "gpt-4"
        assert provider.base_url == "https://api.example.com/v1"

    def test_colon_explicit_provider(self):
        config = _make_config(providers={
            "openrouter": Provider(base_url="https://or.com/v1"),
            "ollama": Provider(base_url="http://localhost:11434/v1"),
        })
        provider, model = get_provider(config, "ollama:llama3")
        assert model == "llama3"
        assert provider.base_url == "http://localhost:11434/v1"

    def test_colon_unknown_provider_falls_through(self):
        """Colon with non-provider prefix uses first provider and full string."""
        config = _make_config()
        provider, model = get_provider(config, "google/gemini-2.5-flash-lite:online")
        assert model == "google/gemini-2.5-flash-lite:online"
        assert provider.base_url == "https://api.example.com/v1"

    def test_no_providers_raises(self):
        config = _make_config(providers={})
        with pytest.raises(LLMError, match="No providers configured"):
            get_provider(config, "gpt-4")

    def test_colon_in_model_name_splits_once(self):
        """Model strings like 'ollama:ns/model:tag' split on first colon only."""
        config = _make_config(providers={
            "ollama": Provider(base_url="http://localhost:11434/v1"),
        })
        provider, model = get_provider(config, "ollama:ns/model:latest")
        assert model == "ns/model:latest"
        assert provider.base_url == "http://localhost:11434/v1"

    def test_all_model_defaults_resolve(self):
        """M252: all MODEL_DEFAULTS resolve via a single gateway provider."""
        from kiso.config import MODEL_DEFAULTS
        config = _make_config()
        for role, model_str in MODEL_DEFAULTS.items():
            provider, model_name = get_provider(config, model_str)
            assert provider.base_url == "https://api.example.com/v1", (
                f"Role {role!r} model {model_str!r} should use the gateway provider"
            )
            assert model_name == model_str, (
                f"Role {role!r}: model name should pass through as-is"
            )


# --- _get_api_key ---

class TestGetApiKey:
    def test_returns_key_when_set(self):
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-secret"}):
            assert _get_api_key() == "sk-secret"

    def test_returns_none_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KISO_LLM_API_KEY", None)
            assert _get_api_key() is None


# --- call_llm ---

class TestCallLlm:
    @pytest.mark.asyncio
    async def test_no_model_for_role_raises(self):
        config = _make_config(models={})
        with pytest.raises(LLMError, match="No model configured for role 'planner'"):
            await call_llm(config, "planner", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_structured_role_without_format_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with pytest.raises(LLMError, match="requires structured output"):
                await call_llm(config, "planner", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_non_structured_role_without_format_ok(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream("response text"))
                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                assert result == "response text"

    @pytest.mark.asyncio
    async def test_successful_call_returns_content(self):
        config = _make_config()
        schema = {"type": "json_schema", "json_schema": {"name": "test"}}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream('{"goal":"test"}'))
                result = await call_llm(config, "planner", [{"role": "user", "content": "hi"}], response_format=schema)
                assert result == '{"goal":"test"}'

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = httpx.TimeoutException("timeout")
                with pytest.raises(LLMError, match="timed out"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_request_error_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = httpx.ConnectError("refused")
                with pytest.raises(LLMError, match="request failed"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_non_200_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _error_stream(429, "rate limited"))
                with pytest.raises(LLMError, match="429.*rate limited"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_400_error_hint(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _error_stream(400, ""))
                with pytest.raises(LLMError, match="400.*model may be unavailable"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_401_error_hint(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _error_stream(401, "unauthorized"))
                with pytest.raises(LLMError, match="401.*check your API key"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_402_error_hint(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _error_stream(402, "payment required"))
                with pytest.raises(LLMError, match="402.*insufficient credits"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_empty_content_raises_error(self):
        """Stream with no content chunks raises Empty response error."""
        config = _make_config()
        # Send only a finish chunk with no content
        lines = [
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _StreamCM(_MockStreamResp(200, lines)))
                with pytest.raises(LLMError, match="Empty response"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_auth_header_sent_when_api_key(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                call_kwargs = mock_client.stream.call_args[1]
                assert call_kwargs["headers"].get("Authorization") == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_no_auth_header_without_api_key(self):
        config = _make_config(providers={
            "local": Provider(base_url="http://localhost:11434/v1"),
        }, models={"worker": "llama3"})
        with patch.dict(os.environ, {}, clear=True), \
             patch("kiso.llm.httpx.AsyncClient") as mock_cls:
            mock_client = _setup_mock(mock_cls, _ok_stream())
            await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
            call_kwargs = mock_client.stream.call_args[1]
            assert "Authorization" not in call_kwargs["headers"]

    @pytest.mark.asyncio
    async def test_response_format_in_payload(self):
        config = _make_config()
        schema = {"type": "json_schema", "json_schema": {"name": "plan"}}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream('{}'))
                await call_llm(config, "planner", [{"role": "user", "content": "hi"}], response_format=schema)
                payload = mock_client.stream.call_args[1]["json"]
                assert payload["response_format"] == schema

    @pytest.mark.asyncio
    async def test_url_construction(self):
        """base_url trailing slash is stripped before appending /chat/completions."""
        config = _make_config(providers={
            "local": Provider(base_url="http://localhost:11434/v1/"),
        }, models={"worker": "llama3"})
        with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
            mock_client = _setup_mock(mock_cls, _ok_stream())
            await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
            # stream("POST", url, ...) — url is the second positional arg
            url = mock_client.stream.call_args[0][1]
            assert url == "http://localhost:11434/v1/chat/completions"
            assert "//" not in url.split("://")[1]

    @pytest.mark.asyncio
    async def test_stream_true_in_payload(self):
        """M299: payload always includes stream=True and stream_options."""
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                payload = mock_client.stream.call_args[1]["json"]
                assert payload["stream"] is True
                assert payload["stream_options"] == {"include_usage": True}


# --- Audit logging ---


class TestCallLlmAudit:
    @pytest.mark.asyncio
    async def test_audit_logged_on_success(self):
        config = _make_config()
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                _setup_mock(mock_cls, _ok_stream("ok", usage=usage))
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}], session="sess1")
                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[0] == "sess1"  # session
                assert args[1] == "worker"  # role
                assert args[2] == "gpt-3.5"  # model
                assert args[3] == "openrouter"  # provider
                assert args[4] == 100  # input_tokens
                assert args[5] == 50  # output_tokens
                assert isinstance(args[6], int)  # duration_ms
                assert args[7] == "ok"  # status

    @pytest.mark.asyncio
    async def test_audit_logged_on_error(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = httpx.TimeoutException("timeout")
                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}], session="s1")
                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_logged_on_non_200(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                _setup_mock(mock_cls, _error_stream(500, "error"))
                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_logged_on_request_error(self):
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = httpx.ConnectError("refused")
                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}], session="s1")
                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[0] == "s1"  # session
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_logged_on_empty_response(self):
        """Empty content triggers error audit."""
        config = _make_config()
        lines = [
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                _setup_mock(mock_cls, _StreamCM(_MockStreamResp(200, lines)))
                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}], session="s1")
                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_defaults_tokens_to_zero(self):
        """When no usage in stream, tokens default to 0."""
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                _setup_mock(mock_cls, _ok_stream("ok"))  # no usage
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                args = mock_audit.call_args[0]
                assert args[4] == 0  # input_tokens
                assert args[5] == 0  # output_tokens


# --- Timeout uses config value ---


class TestTimeoutConfig:
    @pytest.mark.asyncio
    async def test_timeout_uses_config_value(self):
        """Verify stream request receives timeout from llm_timeout config."""
        config = _make_config(settings={"llm_timeout": 42})
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("ok"))
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                call_kwargs = mock_client.stream.call_args[1]
                assert call_kwargs["timeout"] == 42


# --- LLM budget tracking ---


class TestLLMBudget:
    def test_set_and_get_budget(self):
        set_llm_budget(10)
        assert get_llm_call_count() == 0
        clear_llm_budget()

    def test_clear_resets_budget(self):
        set_llm_budget(5)
        clear_llm_budget()
        # After clearing, no budget is active — calls should not raise
        assert get_llm_call_count() == 0

    @pytest.mark.asyncio
    async def test_budget_increments_on_call(self):
        config = _make_config()
        set_llm_budget(10)
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("ok"))
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                assert get_llm_call_count() == 1
                # Reset stream mock for second call
                mock_client.stream.return_value = _ok_stream("ok")
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                assert get_llm_call_count() == 2
        clear_llm_budget()

    @pytest.mark.asyncio
    async def test_budget_exceeded_raises(self):
        config = _make_config()
        set_llm_budget(1)
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream("ok"))
                # First call succeeds (uses the 1 allowed call)
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                # Second call exceeds budget
                with pytest.raises(LLMBudgetExceeded, match="budget exhausted"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
        clear_llm_budget()

    @pytest.mark.asyncio
    async def test_no_budget_allows_unlimited(self):
        """When no budget is set, calls are unlimited."""
        config = _make_config()
        clear_llm_budget()  # Ensure no budget
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("ok"))
                for i in range(5):
                    mock_client.stream.return_value = _ok_stream("ok")
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
        clear_llm_budget()

    @pytest.mark.asyncio
    async def test_budget_exceeded_before_http_call(self):
        """Budget check happens before making any HTTP request."""
        config = _make_config()
        set_llm_budget(0)  # Zero budget — no calls allowed
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                with pytest.raises(LLMBudgetExceeded):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                # HTTP client should NOT have been called
                mock_client.stream.assert_not_called()
        clear_llm_budget()


# --- Token usage tracking ---


class TestUsageTracking:
    def test_reset_clears_accumulator(self):
        reset_usage_tracking()
        summary = get_usage_summary()
        assert summary["input_tokens"] == 0
        assert summary["output_tokens"] == 0
        assert summary["model"] is None

    @pytest.mark.asyncio
    async def test_usage_accumulates(self):
        config = _make_config()
        reset_usage_tracking()
        usage1 = {"prompt_tokens": 100, "completion_tokens": 50}
        usage2 = {"prompt_tokens": 200, "completion_tokens": 80}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("r1", usage=usage1))
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

                mock_client.stream.return_value = _ok_stream("r2", usage=usage2)
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

        summary = get_usage_summary()
        assert summary["input_tokens"] == 300
        assert summary["output_tokens"] == 130
        assert summary["model"] == "gpt-3.5"

    def test_get_usage_summary_without_reset(self):
        """When no tracking started, returns zeros."""
        from kiso.llm import _llm_usage_entries
        _llm_usage_entries.set(None)
        summary = get_usage_summary()
        assert summary["input_tokens"] == 0
        assert summary["output_tokens"] == 0
        assert summary["model"] is None

    def test_get_usage_index_empty(self):
        """Index is 0 immediately after reset (no entries yet)."""
        reset_usage_tracking()
        assert get_usage_index() == 0

    @pytest.mark.asyncio
    async def test_get_usage_index_after_entries(self):
        """Index equals the number of accumulated entries."""
        config = _make_config()
        reset_usage_tracking()
        usage1 = {"prompt_tokens": 10, "completion_tokens": 5}
        usage2 = {"prompt_tokens": 20, "completion_tokens": 8}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("r1", usage=usage1))
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                assert get_usage_index() == 1

                mock_client.stream.return_value = _ok_stream("r2", usage=usage2)
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                assert get_usage_index() == 2

    @pytest.mark.asyncio
    async def test_get_usage_since_subset(self):
        """get_usage_since returns correct delta for a slice of entries."""
        config = _make_config()
        reset_usage_tracking()
        usages = [
            {"prompt_tokens": 100, "completion_tokens": 10},
            {"prompt_tokens": 200, "completion_tokens": 20},
            {"prompt_tokens": 300, "completion_tokens": 30},
        ]
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("r1", usage=usages[0]))
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

                # Snapshot index after first call
                idx = get_usage_index()
                assert idx == 1

                # Two more calls
                mock_client.stream.return_value = _ok_stream("r2", usage=usages[1])
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

                mock_client.stream.return_value = _ok_stream("r3", usage=usages[2])
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

        delta = get_usage_since(idx)
        # Should only include entries 1 and 2 (200+300=500 in, 20+30=50 out)
        assert delta["input_tokens"] == 500
        assert delta["output_tokens"] == 50
        assert delta["model"] == "gpt-3.5"
        # calls key contains per-call entries
        assert "calls" in delta
        assert len(delta["calls"]) == 2
        for call in delta["calls"]:
            assert "role" in call
            assert "model" in call
            assert "input_tokens" in call
            assert "output_tokens" in call
            assert "messages" in call
            assert "response" in call

    @pytest.mark.asyncio
    async def test_usage_entries_contain_messages_and_response(self):
        """Verify _llm_usage_entries now includes messages and response fields."""
        config = _make_config()
        reset_usage_tracking()
        messages = [{"role": "user", "content": "hi there"}]
        usage = {"prompt_tokens": 50, "completion_tokens": 25}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream("hello back", usage=usage))
                await call_llm(config, "worker", messages)

        from kiso.llm import _llm_usage_entries
        entries = _llm_usage_entries.get(None)
        assert entries is not None
        assert len(entries) == 1
        entry = entries[0]
        assert entry["messages"] == [{"role": "user", "content": "hi there"}]
        assert entry["response"] == "hello back"

    @pytest.mark.asyncio
    async def test_usage_entries_contain_duration_ms(self):
        """Verify _llm_usage_entries includes duration_ms field."""
        config = _make_config()
        reset_usage_tracking()
        messages = [{"role": "user", "content": "hi"}]
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream("ok", usage=usage))
                await call_llm(config, "worker", messages)

        from kiso.llm import _llm_usage_entries
        entries = _llm_usage_entries.get(None)
        assert entries is not None
        entry = entries[0]
        assert "duration_ms" in entry
        assert isinstance(entry["duration_ms"], int)
        assert entry["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_get_usage_since_includes_messages_and_response(self):
        """Verify get_usage_since()['calls'] entries contain messages and response."""
        config = _make_config()
        reset_usage_tracking()
        messages = [{"role": "user", "content": "test prompt"}]
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream("test response", usage=usage))
                await call_llm(config, "worker", messages)

        delta = get_usage_since(0)
        assert len(delta["calls"]) == 1
        call = delta["calls"][0]
        assert call["messages"] == [{"role": "user", "content": "test prompt"}]
        assert call["response"] == "test response"


# --- Shared HTTP client (M61a) ---


class TestSharedHttpClient:
    @pytest.mark.asyncio
    async def test_shared_client_used_when_set(self):
        """When _http_client is set, call_llm uses it directly without creating a new one."""
        import kiso.llm as llm_mod

        config = _make_config()
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=_ok_stream("shared client response"))

        prev = llm_mod._http_client
        try:
            llm_mod._http_client = mock_client
            with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}), \
                 patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                # Shared client was used — AsyncClient constructor NOT called
                mock_cls.assert_not_called()
            assert result == "shared client response"
        finally:
            llm_mod._http_client = prev

    @pytest.mark.asyncio
    async def test_fallback_per_call_client_when_none(self):
        """When _http_client is None, call_llm creates a per-call AsyncClient."""
        import kiso.llm as llm_mod

        config = _make_config()
        prev = llm_mod._http_client
        try:
            llm_mod._http_client = None
            with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}), \
                 patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("fallback response"))
                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                mock_cls.assert_called_once()
            assert result == "fallback response"
        finally:
            llm_mod._http_client = prev

    @pytest.mark.asyncio
    async def test_init_and_close_http_client(self):
        """init_http_client sets _http_client; close_http_client clears it."""
        import kiso.llm as llm_mod

        prev = llm_mod._http_client
        try:
            await init_http_client(timeout=30.0)
            assert llm_mod._http_client is not None
            await close_http_client()
            assert llm_mod._http_client is None
        finally:
            llm_mod._http_client = prev

    @pytest.mark.asyncio
    async def test_init_http_client_twice_closes_old(self):
        """Calling init_http_client twice closes the previous client first."""
        import kiso.llm as llm_mod

        prev = llm_mod._http_client
        try:
            await init_http_client(timeout=30.0)
            first_client = llm_mod._http_client
            first_client.aclose = AsyncMock()

            await init_http_client(timeout=60.0)
            second_client = llm_mod._http_client

            assert second_client is not first_client
            first_client.aclose.assert_awaited_once()
        finally:
            await close_http_client()
            llm_mod._http_client = prev


# --- Thinking/reasoning extraction (M98a) ---


class TestThinkingExtraction:
    """Verify call_llm extracts thinking from streaming deltas and <think> tags."""

    @pytest.mark.asyncio
    async def test_reasoning_content_from_stream(self):
        """Streaming reasoning_content deltas are stored as 'thinking' in usage entry."""
        config = _make_config()
        reset_usage_tracking()
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream(
                    "final answer", usage=usage, reasoning_content="step by step",
                ))
                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

        assert result == "final answer"
        from kiso.llm import _llm_usage_entries
        entry = _llm_usage_entries.get()[-1]
        assert entry["thinking"] == "step by step"
        assert entry["response"] == "final answer"

    @pytest.mark.asyncio
    async def test_think_tags_extracted_from_content(self):
        """<think> tags are extracted; clean content returned and stored."""
        config = _make_config()
        reset_usage_tracking()
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream(
                    "<think>reasoning here</think>clean answer", usage=usage,
                ))
                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

        assert result == "clean answer"
        from kiso.llm import _llm_usage_entries
        entry = _llm_usage_entries.get()[-1]
        assert entry["thinking"] == "reasoning here"
        assert entry["response"] == "clean answer"

    @pytest.mark.asyncio
    async def test_tags_take_precedence_over_stream_reasoning(self):
        """When both <think> tags and reasoning_content exist, tags win."""
        config = _make_config()
        reset_usage_tracking()
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream(
                    "<think>tag thinking</think>answer",
                    usage=usage,
                    reasoning_content="api thinking",
                ))
                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

        assert result == "answer"
        from kiso.llm import _llm_usage_entries
        entry = _llm_usage_entries.get()[-1]
        assert entry["thinking"] == "tag thinking"

    @pytest.mark.asyncio
    async def test_no_thinking_present(self):
        """When neither tags nor reasoning_content exist, thinking is empty."""
        config = _make_config()
        reset_usage_tracking()
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream("plain answer", usage=usage))
                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

        assert result == "plain answer"
        from kiso.llm import _llm_usage_entries
        entry = _llm_usage_entries.get()[-1]
        assert entry["thinking"] == ""

    @pytest.mark.asyncio
    async def test_thinking_field_in_usage_since(self):
        """get_usage_since includes thinking field in calls."""
        config = _make_config()
        reset_usage_tracking()
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream(
                    "answer", usage=usage, reasoning_content="deep thought",
                ))
                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

        delta = get_usage_since(0)
        assert delta["calls"][0]["thinking"] == "deep thought"


# --- M105b: max_tokens parameter ---


class TestMaxTokensParam:
    """M105b: call_llm forwards max_tokens to the API payload."""

    @pytest.mark.asyncio
    async def test_max_tokens_in_payload(self):
        """When max_tokens is set, it appears in the request payload."""
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("echo hi"))
                await call_llm(
                    config, "worker",
                    [{"role": "user", "content": "hi"}],
                    max_tokens=500,
                )
                payload = mock_client.stream.call_args[1]["json"]
                assert payload["max_tokens"] == 500

    @pytest.mark.asyncio
    async def test_max_tokens_none_uses_role_default(self):
        """M296: When max_tokens is None, the role default from MAX_TOKENS_DEFAULTS applies."""
        from kiso.config import MAX_TOKENS_DEFAULTS
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream("echo hi"))
                await call_llm(
                    config, "worker",
                    [{"role": "user", "content": "hi"}],
                )
                payload = mock_client.stream.call_args[1]["json"]
                assert payload["max_tokens"] == MAX_TOKENS_DEFAULTS["worker"]


# --- Inflight call tracking (M109c) ---


class TestInflightCallTracking:
    @pytest.mark.asyncio
    async def test_inflight_set_during_call(self):
        """Inflight entry is populated while the streaming request is in progress."""
        config = _make_config()
        captured_inflight = {}

        def _capture_stream(*args, **kwargs):
            # Capture inflight state when stream() is called (inflight already set)
            captured_inflight.update(_inflight_calls.get("test-sess", {}))
            return _ok_stream("done")

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = _capture_stream
                await call_llm(
                    config, "worker",
                    [{"role": "user", "content": "hello"}],
                    session="test-sess",
                )

        assert captured_inflight["role"] == "worker"
        assert captured_inflight["model"] == "gpt-3.5"
        assert len(captured_inflight["messages"]) == 1
        assert captured_inflight["messages"][0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_inflight_cleared_after_success(self):
        """After a successful call, inflight entry is removed."""
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream("done"))
                await call_llm(
                    config, "worker",
                    [{"role": "user", "content": "hi"}],
                    session="cleared-sess",
                )

        assert get_inflight_call("cleared-sess") is None

    @pytest.mark.asyncio
    async def test_inflight_cleared_on_timeout(self):
        """Inflight entry is cleaned up even when the call times out."""
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = httpx.TimeoutException("timed out")
                with pytest.raises(LLMError, match="timed out"):
                    await call_llm(
                        config, "worker",
                        [{"role": "user", "content": "hi"}],
                        session="timeout-sess",
                    )

        assert get_inflight_call("timeout-sess") is None

    @pytest.mark.asyncio
    async def test_inflight_cleared_on_http_error(self):
        """Inflight entry is cleaned up on HTTP errors."""
        config = _make_config()
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = httpx.RequestError("connection failed")
                with pytest.raises(LLMError, match="request failed"):
                    await call_llm(
                        config, "worker",
                        [{"role": "user", "content": "hi"}],
                        session="error-sess",
                    )

        assert get_inflight_call("error-sess") is None

    def test_get_inflight_call_returns_none_when_empty(self):
        """get_inflight_call returns None for unknown sessions."""
        assert get_inflight_call("nonexistent-session") is None

    @pytest.mark.asyncio
    async def test_no_inflight_without_session(self):
        """When session is empty, no inflight entry is created."""
        config = _make_config()
        captured_keys = []

        def _capture_stream(*args, **kwargs):
            captured_keys.extend(_inflight_calls.keys())
            return _ok_stream("done")

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = _capture_stream
                await call_llm(
                    config, "worker",
                    [{"role": "user", "content": "hi"}],
                    session="",
                )

        assert "" not in captured_keys


class TestJsonSchemaFallback:
    """M262: json_schema → json_object fallback for incompatible models."""

    _SCHEMA = {"type": "json_schema", "json_schema": {"name": "review", "strict": True, "schema": {"type": "object"}}}

    def setup_method(self):
        _json_object_only_models.discard("gpt-4")
        _json_object_only_models.discard("gpt-3.5")

    def teardown_method(self):
        _json_object_only_models.discard("gpt-4")
        _json_object_only_models.discard("gpt-3.5")

    @pytest.mark.asyncio
    async def test_fallback_on_response_format_400(self):
        """400 with 'response_format' in body triggers json_object retry."""
        config = _make_config()
        call_count = 0

        def _mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            payload = kwargs.get("json", {})
            rf = payload.get("response_format", {})
            if rf.get("type") == "json_schema":
                return _error_stream(
                    400,
                    '{"error":{"message":"Request param: response_format is invalid"}}',
                )
            # json_object succeeds
            assert rf.get("type") == "json_object"
            return _ok_stream('{"status":"ok"}')

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = _mock_stream
                result = await call_llm(
                    config, "planner",
                    [{"role": "user", "content": "hi"}],
                    response_format=self._SCHEMA,
                )

        assert result == '{"status":"ok"}'
        assert call_count == 2  # first json_schema → 400, then json_object → 200
        assert "gpt-4" in _json_object_only_models

    @pytest.mark.asyncio
    async def test_cache_skips_json_schema_on_second_call(self):
        """After caching, subsequent calls go directly to json_object."""
        config = _make_config()
        _json_object_only_models.add("gpt-4")
        payloads = []

        def _mock_stream(*args, **kwargs):
            payloads.append(kwargs.get("json", {}))
            return _ok_stream('{"status":"ok"}')

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = _mock_stream
                await call_llm(
                    config, "planner",
                    [{"role": "user", "content": "hi"}],
                    response_format=self._SCHEMA,
                )

        assert len(payloads) == 1
        assert payloads[0]["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_non_matching_400_not_retried(self):
        """400 without 'response_format' in body raises normally."""
        config = _make_config()

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _error_stream(
                    400, '{"error":{"message":"Invalid model specified"}}',
                ))
                with pytest.raises(LLMError, match="Invalid model"):
                    await call_llm(
                        config, "planner",
                        [{"role": "user", "content": "hi"}],
                        response_format=self._SCHEMA,
                    )

        assert "gpt-4" not in _json_object_only_models

    @pytest.mark.asyncio
    async def test_no_fallback_without_response_format(self):
        """400 on non-structured call doesn't trigger fallback logic."""
        config = _make_config()

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _error_stream(
                    400, '{"error":{"message":"response_format is invalid"}}',
                ))
                with pytest.raises(LLMError):
                    await call_llm(
                        config, "worker",
                        [{"role": "user", "content": "hi"}],
                    )

    @pytest.mark.asyncio
    async def test_json_object_format_not_downgraded(self):
        """Calls already using json_object are not affected by fallback."""
        config = _make_config()
        json_object_format = {"type": "json_object"}

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                _setup_mock(mock_cls, _ok_stream('{"result": true}'))
                result = await call_llm(
                    config, "planner",
                    [{"role": "user", "content": "hi"}],
                    response_format=json_object_format,
                )

        assert result == '{"result": true}'


# --- M271: Reasoning budget control per role ---


class TestM271ReasoningDefaults:
    """M271: per-role reasoning config is included in API payload."""

    @pytest.mark.asyncio
    async def test_messenger_includes_reasoning(self):
        """Messenger role sends reasoning config in payload."""
        config = _make_config()
        captured_payload: list[dict] = []

        def _capture(*args, **kwargs):
            captured_payload.append(kwargs.get("json", {}))
            return _ok_stream("Hello user")

        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = _capture
                await call_llm(config, "messenger", [{"role": "user", "content": "hi"}])

        assert len(captured_payload) == 1
        assert "reasoning" in captured_payload[0]
        assert captured_payload[0]["reasoning"]["effort"] == "low"

    @pytest.mark.asyncio
    async def test_planner_no_reasoning(self):
        """Planner role does NOT send reasoning config (not in REASONING_DEFAULTS)."""
        config = _make_config()
        captured_payload: list[dict] = []

        def _capture(*args, **kwargs):
            captured_payload.append(kwargs.get("json", {}))
            return _ok_stream('{"goal":"test","secrets":null,"tasks":[]}')

        schema = {"type": "json_schema", "json_schema": {"name": "test"}}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = _setup_mock(mock_cls, _ok_stream())
                mock_client.stream.side_effect = _capture
                await call_llm(
                    config, "planner",
                    [{"role": "user", "content": "hi"}],
                    response_format=schema,
                )

        assert len(captured_payload) == 1
        assert "reasoning" not in captured_payload[0]


def test_m271_reasoning_defaults_import():
    """REASONING_DEFAULTS is importable from config and has expected structure."""
    from kiso.config import REASONING_DEFAULTS
    assert isinstance(REASONING_DEFAULTS, dict)
    assert "messenger" in REASONING_DEFAULTS
    assert REASONING_DEFAULTS["messenger"]["effort"] == "low"
    # Roles not in the dict get no reasoning


# --- M299: SSE stream parsing ---


class TestM299SSEStreamParsing:
    """M299: _read_sse_stream correctly parses OpenAI-compatible SSE."""

    @pytest.mark.asyncio
    async def test_multi_chunk_content(self):
        """Multiple content chunks are reassembled."""
        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"index":0}]}',
            'data: {"choices":[{"delta":{"content":" world"},"index":0}]}',
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}',
            "data: [DONE]",
        ]
        from kiso.llm import _read_sse_stream
        resp = _MockStreamResp(200, lines)
        content, reasoning, pt, ct = await _read_sse_stream(resp)
        assert content == "Hello world"
        assert reasoning == ""
        assert pt == 10
        assert ct == 5

    @pytest.mark.asyncio
    async def test_reasoning_chunks(self):
        """reasoning_content deltas are reassembled."""
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"step 1"},"index":0}]}',
            'data: {"choices":[{"delta":{"reasoning_content":" step 2"},"index":0}]}',
            'data: {"choices":[{"delta":{"content":"answer"},"index":0}]}',
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        from kiso.llm import _read_sse_stream
        resp = _MockStreamResp(200, lines)
        content, reasoning, pt, ct = await _read_sse_stream(resp)
        assert content == "answer"
        assert reasoning == "step 1 step 2"

    @pytest.mark.asyncio
    async def test_comment_and_empty_lines_ignored(self):
        """SSE comments (: keepalive) and empty lines are skipped."""
        lines = [
            ": keepalive",
            "",
            'data: {"choices":[{"delta":{"content":"ok"},"index":0}]}',
            "",
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        from kiso.llm import _read_sse_stream
        resp = _MockStreamResp(200, lines)
        content, _, _, _ = await _read_sse_stream(resp)
        assert content == "ok"

    @pytest.mark.asyncio
    async def test_malformed_json_chunks_skipped(self):
        """Invalid JSON in data lines is silently skipped."""
        lines = [
            "data: {not valid json}",
            'data: {"choices":[{"delta":{"content":"ok"},"index":0}]}',
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        from kiso.llm import _read_sse_stream
        resp = _MockStreamResp(200, lines)
        content, _, _, _ = await _read_sse_stream(resp)
        assert content == "ok"

    @pytest.mark.asyncio
    async def test_usage_from_final_chunk(self):
        """Usage info from the final chunk is captured."""
        lines = [
            'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":7}}',
            "data: [DONE]",
        ]
        from kiso.llm import _read_sse_stream
        resp = _MockStreamResp(200, lines)
        _, _, pt, ct = await _read_sse_stream(resp)
        assert pt == 42
        assert ct == 7
