"""M1374: pre-list_methods auth hook in MCP transports.

Tests for `kiso.mcp.auth.resolve_auth()` and for the token
injection in both `MCPStdioClient` and `MCPStreamableHTTPClient`.

`resolve_auth` is a sync function that:
- returns None when `server.auth` is None or missing `type`
- returns an access_token string when `server.auth` has
  `type="device_flow"` and a valid cached credential exists
- raises `MCPConfigError` for unsupported auth types
- runs the interactive device flow when no cached credential
  is available (mocked in tests)
- re-runs the flow when the cached credential is expired

Token injection:
- stdio: `_build_env()` includes `OAUTH_TOKEN=<token>` when a
  token was resolved
- HTTP: `_base_headers()` includes `Authorization: Bearer <token>`
  when a token was resolved
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from kiso.mcp.config import MCPServer, MCPConfigError


# ---------------------------------------------------------------------------
# resolve_auth
# ---------------------------------------------------------------------------


def _server(auth: dict | None = None) -> MCPServer:
    return MCPServer(
        name="test_server",
        transport="stdio",
        command="echo",
        auth=auth,
    )


class TestResolveAuth:
    def test_no_auth_returns_none(self) -> None:
        from kiso.mcp.auth import resolve_auth

        assert resolve_auth(_server(auth=None)) is None

    def test_auth_without_type_returns_none(self) -> None:
        from kiso.mcp.auth import resolve_auth

        assert resolve_auth(_server(auth={"foo": "bar"})) is None

    def test_unsupported_type_raises(self) -> None:
        from kiso.mcp.auth import resolve_auth

        with pytest.raises(MCPConfigError, match="unsupported auth type"):
            resolve_auth(_server(auth={"type": "magic_token"}))

    def test_cached_token_returned(self) -> None:
        from kiso.mcp.auth import resolve_auth

        cached = {
            "access_token": "ghp_cached",
            "token_type": "bearer",
            "saved_at": time.time(),
        }
        with patch("kiso.mcp.auth.load_credential", return_value=cached):
            token = resolve_auth(
                _server(
                    auth={
                        "type": "device_flow",
                        "client_id": "cid",
                        "device_code_url": "https://example/code",
                        "token_url": "https://example/token",
                    }
                )
            )
        assert token == "ghp_cached"

    def test_expired_token_triggers_flow(self) -> None:
        from kiso.mcp.auth import resolve_auth

        expired = {
            "access_token": "ghp_old",
            "token_type": "bearer",
            "saved_at": time.time() - 99999,
            "expires_in": 3600,
        }
        fresh_token = {
            "access_token": "ghp_fresh",
            "token_type": "bearer",
        }
        from kiso.mcp.oauth import DeviceFlowResponse

        mock_dfr = DeviceFlowResponse(
            device_code="DC",
            user_code="UC",
            verification_uri="https://example/verify",
            expires_in=900,
            interval=1,
        )
        with (
            patch("kiso.mcp.auth.load_credential", return_value=expired),
            patch(
                "kiso.mcp.auth.request_device_code", return_value=mock_dfr
            ),
            patch(
                "kiso.mcp.auth.poll_for_token", return_value=fresh_token
            ),
            patch("kiso.mcp.auth.save_credential") as mock_save,
        ):
            token = resolve_auth(
                _server(
                    auth={
                        "type": "device_flow",
                        "client_id": "cid",
                        "device_code_url": "https://example/code",
                        "token_url": "https://example/token",
                    }
                )
            )
        assert token == "ghp_fresh"
        mock_save.assert_called_once()

    def test_missing_credential_triggers_flow(self) -> None:
        from kiso.mcp.auth import resolve_auth
        from kiso.mcp.oauth import DeviceFlowResponse

        mock_dfr = DeviceFlowResponse(
            device_code="DC",
            user_code="UC",
            verification_uri="https://example/verify",
            expires_in=900,
            interval=1,
        )
        fresh_token = {"access_token": "ghp_new", "token_type": "bearer"}
        with (
            patch("kiso.mcp.auth.load_credential", return_value=None),
            patch(
                "kiso.mcp.auth.request_device_code", return_value=mock_dfr
            ),
            patch(
                "kiso.mcp.auth.poll_for_token", return_value=fresh_token
            ),
            patch("kiso.mcp.auth.save_credential") as mock_save,
        ):
            token = resolve_auth(
                _server(
                    auth={
                        "type": "device_flow",
                        "client_id": "cid",
                        "device_code_url": "https://example/code",
                        "token_url": "https://example/token",
                    }
                )
            )
        assert token == "ghp_new"
        mock_save.assert_called_once_with("test_server", fresh_token)

    def test_missing_client_id_raises(self) -> None:
        from kiso.mcp.auth import resolve_auth

        with pytest.raises(MCPConfigError, match="client_id"):
            resolve_auth(
                _server(
                    auth={
                        "type": "device_flow",
                        "device_code_url": "https://example/code",
                        "token_url": "https://example/token",
                    }
                )
            )


# ---------------------------------------------------------------------------
# Token injection — stdio
# ---------------------------------------------------------------------------


class TestStdioTokenInjection:
    def test_build_env_includes_oauth_token(self) -> None:
        from kiso.mcp.stdio import MCPStdioClient

        server = _server(
            auth={
                "type": "device_flow",
                "client_id": "cid",
                "device_code_url": "https://example/code",
                "token_url": "https://example/token",
            }
        )
        client = MCPStdioClient(server)
        # Simulate resolved auth
        client._auth_token = "ghp_injected"
        env = client._build_env()
        assert env.get("OAUTH_TOKEN") == "ghp_injected"

    def test_build_env_no_oauth_token_when_no_auth(self) -> None:
        from kiso.mcp.stdio import MCPStdioClient

        client = MCPStdioClient(_server())
        env = client._build_env()
        assert "OAUTH_TOKEN" not in env


# ---------------------------------------------------------------------------
# Token injection — HTTP
# ---------------------------------------------------------------------------


class TestHttpTokenInjection:
    def test_base_headers_includes_bearer(self) -> None:
        from kiso.mcp.http import MCPStreamableHTTPClient

        server = MCPServer(
            name="test_http",
            transport="http",
            url="https://example.com/mcp",
            auth={
                "type": "device_flow",
                "client_id": "cid",
                "device_code_url": "https://example/code",
                "token_url": "https://example/token",
            },
        )
        client = MCPStreamableHTTPClient(server)
        client._auth_token = "ghp_injected"
        headers = client._base_headers(with_session=False)
        assert headers.get("Authorization") == "Bearer ghp_injected"

    def test_base_headers_no_bearer_when_no_auth(self) -> None:
        from kiso.mcp.http import MCPStreamableHTTPClient

        server = MCPServer(
            name="test_http",
            transport="http",
            url="https://example.com/mcp",
        )
        client = MCPStreamableHTTPClient(server)
        headers = client._base_headers(with_session=False)
        assert "Authorization" not in headers
