"""Tests for the MCP OAuth device flow client (M1372).

Device flow is the simplest interactive OAuth shape: the client
asks the auth server for a `device_code` + `user_code` + URL,
prints the URL and code to the user, then polls a token endpoint
until the user completes the consent in their browser.

Why we need it for kiso: most "managed" MCP servers (Linear,
GitHub, Atlassian) offer OAuth flows for unattended consumers
that cannot run a browser locally. Device flow is the right fit
for kiso's headless Docker runtime — the user opens the URL on
*their* machine while kiso polls.

This test file mocks ``httpx.post`` so the entire flow can be
exercised without hitting any real auth server. The actual
end-to-end test against GitHub lives under ``tests/interactive/``
and requires a human at the terminal.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiso.mcp.oauth import (
    DeviceFlowError,
    DeviceFlowExpired,
    DeviceFlowResponse,
    poll_for_token,
    request_device_code,
)


# ---------------------------------------------------------------------------
# request_device_code
# ---------------------------------------------------------------------------


class TestRequestDeviceCode:
    def test_happy_path_parses_response(self) -> None:
        fake = _FakeResponse(
            200,
            {
                "device_code": "DEVCODE-abc",
                "user_code": "WDJB-MJHT",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            },
        )
        with patch("kiso.mcp.oauth.httpx.post", return_value=fake) as m:
            result = request_device_code(
                device_code_url="https://github.com/login/device/code",
                client_id="cid",
                scopes="repo user",
            )
        assert isinstance(result, DeviceFlowResponse)
        assert result.device_code == "DEVCODE-abc"
        assert result.user_code == "WDJB-MJHT"
        assert result.verification_uri == "https://github.com/login/device"
        assert result.expires_in == 900
        assert result.interval == 5
        # Posts client_id + scope (GitHub uses 'scope', not 'scopes')
        call = m.call_args
        assert call.kwargs["data"]["client_id"] == "cid"
        assert call.kwargs["data"]["scope"] == "repo user"

    def test_http_error_raises_device_flow_error(self) -> None:
        fake = _FakeResponse(500, {"error": "internal_server_error"})
        with patch("kiso.mcp.oauth.httpx.post", return_value=fake):
            with pytest.raises(DeviceFlowError) as exc:
                request_device_code(
                    device_code_url="https://example/code",
                    client_id="cid",
                    scopes="repo",
                )
        assert "500" in str(exc.value) or "internal_server_error" in str(exc.value)

    def test_missing_device_code_field_raises(self) -> None:
        fake = _FakeResponse(200, {"user_code": "ABCD"})  # no device_code
        with patch("kiso.mcp.oauth.httpx.post", return_value=fake):
            with pytest.raises(DeviceFlowError):
                request_device_code(
                    device_code_url="https://example/code",
                    client_id="cid",
                    scopes="repo",
                )


# ---------------------------------------------------------------------------
# poll_for_token
# ---------------------------------------------------------------------------


class TestPollForToken:
    def test_immediate_success(self) -> None:
        fake = _FakeResponse(
            200,
            {
                "access_token": "ghp_real_token",
                "token_type": "bearer",
                "scope": "repo",
            },
        )
        with patch("kiso.mcp.oauth.httpx.post", return_value=fake), patch(
            "kiso.mcp.oauth.time.sleep"
        ):
            token = poll_for_token(
                token_url="https://github.com/login/oauth/access_token",
                client_id="cid",
                device_code="DEVCODE-abc",
                interval=1,
                expires_in=10,
            )
        assert token["access_token"] == "ghp_real_token"
        assert token["token_type"] == "bearer"

    def test_authorization_pending_then_success(self) -> None:
        responses = [
            _FakeResponse(200, {"error": "authorization_pending"}),
            _FakeResponse(200, {"error": "authorization_pending"}),
            _FakeResponse(200, {"access_token": "ghp_x", "token_type": "bearer"}),
        ]
        with patch(
            "kiso.mcp.oauth.httpx.post", side_effect=responses
        ), patch("kiso.mcp.oauth.time.sleep"):
            token = poll_for_token(
                token_url="https://example/token",
                client_id="cid",
                device_code="DEVCODE-abc",
                interval=1,
                expires_in=30,
            )
        assert token["access_token"] == "ghp_x"

    def test_slow_down_increases_interval(self) -> None:
        responses = [
            _FakeResponse(200, {"error": "slow_down"}),
            _FakeResponse(200, {"access_token": "ghp_x", "token_type": "bearer"}),
        ]
        sleeps: list[float] = []
        with patch(
            "kiso.mcp.oauth.httpx.post", side_effect=responses
        ), patch("kiso.mcp.oauth.time.sleep", side_effect=lambda s: sleeps.append(s)):
            poll_for_token(
                token_url="https://example/token",
                client_id="cid",
                device_code="DEVCODE-abc",
                interval=2,
                expires_in=60,
            )
        # After slow_down, the next sleep must be larger than the original
        # interval (RFC 8628 §3.5).
        assert any(s > 2 for s in sleeps)

    def test_expired_token_raises(self) -> None:
        fake = _FakeResponse(200, {"error": "expired_token"})
        with patch("kiso.mcp.oauth.httpx.post", return_value=fake), patch(
            "kiso.mcp.oauth.time.sleep"
        ):
            with pytest.raises(DeviceFlowExpired):
                poll_for_token(
                    token_url="https://example/token",
                    client_id="cid",
                    device_code="DEVCODE-abc",
                    interval=1,
                    expires_in=5,
                )

    def test_access_denied_raises(self) -> None:
        fake = _FakeResponse(200, {"error": "access_denied"})
        with patch("kiso.mcp.oauth.httpx.post", return_value=fake), patch(
            "kiso.mcp.oauth.time.sleep"
        ):
            with pytest.raises(DeviceFlowError) as exc:
                poll_for_token(
                    token_url="https://example/token",
                    client_id="cid",
                    device_code="DEVCODE-abc",
                    interval=1,
                    expires_in=30,
                )
        assert "access_denied" in str(exc.value)

    def test_overall_timeout_raises(self) -> None:
        """Polling stops with DeviceFlowExpired when expires_in elapses."""
        fake = _FakeResponse(200, {"error": "authorization_pending"})
        # Simulate time passing each iteration — patch time.monotonic
        ticks = iter([0.0, 1.0, 2.0, 3.0, 100.0, 200.0])
        with patch(
            "kiso.mcp.oauth.httpx.post", return_value=fake
        ), patch("kiso.mcp.oauth.time.sleep"), patch(
            "kiso.mcp.oauth.time.monotonic", side_effect=lambda: next(ticks)
        ):
            with pytest.raises(DeviceFlowExpired):
                poll_for_token(
                    token_url="https://example/token",
                    client_id="cid",
                    device_code="DEVCODE-abc",
                    interval=1,
                    expires_in=10,
                )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        import json

        return json.dumps(self._payload)
