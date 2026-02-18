"""Tests for kiso/llm.py — LLM client."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from kiso.config import Config, Provider
from kiso.llm import LLMError, call_llm, get_provider, _get_api_key


# --- Minimal config fixtures ---

def _make_config(**overrides) -> Config:
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1", api_key_env="TEST_KEY")},
        users={},
        models={"planner": "gpt-4", "worker": "gpt-3.5"},
        settings={},
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


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

    def test_colon_unknown_provider_raises(self):
        config = _make_config()
        with pytest.raises(LLMError, match="Provider 'missing' not found"):
            get_provider(config, "missing:model")

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


# --- _get_api_key ---

class TestGetApiKey:
    def test_no_api_key_env_returns_none(self):
        provider = Provider(base_url="http://localhost")
        assert _get_api_key(provider) is None

    def test_env_var_set(self):
        provider = Provider(base_url="http://localhost", api_key_env="TEST_LLM_KEY")
        with patch.dict(os.environ, {"TEST_LLM_KEY": "sk-secret"}):
            assert _get_api_key(provider) == "sk-secret"

    def test_env_var_not_set_raises(self):
        provider = Provider(base_url="http://localhost", api_key_env="MISSING_KEY")
        with patch.dict(os.environ, {}, clear=True):
            # Ensure the key is definitely not in env
            os.environ.pop("MISSING_KEY", None)
            with pytest.raises(LLMError, match="MISSING_KEY.*not set"):
                _get_api_key(provider)


# --- call_llm ---

def _ok_response(content: str = "hello", usage: dict | None = None) -> httpx.Response:
    body: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(
        200,
        json=body,
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )


class TestCallLlm:
    @pytest.mark.asyncio
    async def test_no_model_for_role_raises(self):
        config = _make_config(models={})
        with pytest.raises(LLMError, match="No model configured for role 'planner'"):
            await call_llm(config, "planner", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_structured_role_without_format_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with pytest.raises(LLMError, match="requires structured output"):
                await call_llm(config, "planner", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_non_structured_role_without_format_ok(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response("response text")
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await call_llm(config, "worker", [{"role": "user", "content": "hi"}])
                assert result == "response text"

    @pytest.mark.asyncio
    async def test_successful_call_returns_content(self):
        config = _make_config()
        schema = {"type": "json_schema", "json_schema": {"name": "test"}}
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response('{"goal":"test"}')
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await call_llm(config, "planner", [{"role": "user", "content": "hi"}], response_format=schema)
                assert result == '{"goal":"test"}'

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.TimeoutException("timeout")
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError, match="timed out"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_request_error_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.ConnectError("refused")
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError, match="request failed"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_non_200_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = httpx.Response(
                    429, text="rate limited",
                    request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
                )
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError, match="429"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_malformed_response_raises(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = httpx.Response(
                    200, json={"choices": []},
                    request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
                )
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError, match="Unexpected LLM response"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_auth_header_sent_when_api_key(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response()
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

                call_kwargs = mock_client.post.call_args
                headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
                assert headers.get("Authorization") == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_no_auth_header_without_api_key(self):
        config = _make_config(providers={
            "local": Provider(base_url="http://localhost:11434/v1"),
        }, models={"worker": "llama3"})
        with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = _ok_response()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

            call_kwargs = mock_client.post.call_args
            headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
            assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_response_format_in_payload(self):
        config = _make_config()
        schema = {"type": "json_schema", "json_schema": {"name": "plan"}}
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response('{}')
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await call_llm(config, "planner", [{"role": "user", "content": "hi"}], response_format=schema)

                call_kwargs = mock_client.post.call_args
                payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
                assert payload["response_format"] == schema

    @pytest.mark.asyncio
    async def test_url_construction(self):
        """base_url trailing slash is stripped before appending /chat/completions."""
        config = _make_config(providers={
            "local": Provider(base_url="http://localhost:11434/v1/"),
        }, models={"worker": "llama3"})
        with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = _ok_response()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

            url = mock_client.post.call_args[0][0]
            assert url == "http://localhost:11434/v1/chat/completions"
            assert "//" not in url.split("://")[1]


# --- Audit logging ---


class TestCallLlmAudit:
    @pytest.mark.asyncio
    async def test_audit_logged_on_success(self):
        config = _make_config()
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response("ok", usage=usage)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

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
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.TimeoutException("timeout")
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}], session="s1")

                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_logged_on_non_200(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = AsyncMock()
                mock_client.post.return_value = httpx.Response(
                    500, text="error",
                    request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
                )
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_logged_on_request_error(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.ConnectError("refused")
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}], session="s1")

                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[0] == "s1"  # session
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_logged_on_malformed_response(self):
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = AsyncMock()
                mock_client.post.return_value = httpx.Response(
                    200, json={"choices": []},
                    request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
                )
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}], session="s1")

                mock_audit.assert_called_once()
                args = mock_audit.call_args[0]
                assert args[7] == "error"

    @pytest.mark.asyncio
    async def test_audit_defaults_tokens_to_zero(self):
        """When no usage in response, tokens default to 0."""
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls, \
                 patch("kiso.llm.audit.log_llm_call") as mock_audit:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response("ok")  # no usage
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

                args = mock_audit.call_args[0]
                assert args[4] == 0  # input_tokens
                assert args[5] == 0  # output_tokens


# --- Empty response ---


class TestEmptyResponse:
    @pytest.mark.asyncio
    async def test_empty_content_raises_error(self):
        """Mock LLM returning content: '', verify LLMError raised."""
        config = _make_config()
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response("")
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError, match="Empty response"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_none_content_raises_error(self):
        """Mock LLM returning content: None, verify LLMError raised."""
        config = _make_config()
        body = {"choices": [{"message": {"content": None}}]}
        resp = httpx.Response(
            200, json=body,
            request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
        )
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = resp
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                with pytest.raises(LLMError, match="Empty response"):
                    await call_llm(config, "worker", [{"role": "user", "content": "hi"}])


# --- Timeout uses config value ---


class TestTimeoutConfig:
    @pytest.mark.asyncio
    async def test_timeout_uses_config_value(self):
        """Verify httpx.AsyncClient receives timeout from exec_timeout config."""
        config = _make_config(settings={"exec_timeout": 42})
        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}):
            with patch("kiso.llm.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = _ok_response("ok")
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await call_llm(config, "worker", [{"role": "user", "content": "hi"}])

                mock_cls.assert_called_once_with(timeout=42)
