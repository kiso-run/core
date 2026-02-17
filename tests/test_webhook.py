"""Tests for kiso/webhook.py — URL validation and delivery."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from kiso.webhook import validate_webhook_url, deliver_webhook


# --- validate_webhook_url ---


class TestValidateWebhookUrl:
    def test_valid_https(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            validate_webhook_url("https://example.com/callback")

    def test_valid_http(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            validate_webhook_url("http://example.com/callback")

    def test_reject_ftp_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_webhook_url("ftp://example.com/file")

    def test_reject_no_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_webhook_url("example.com/callback")

    def test_reject_loopback_127(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://localhost/callback")

    def test_reject_private_10(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://internal.corp/callback")

    def test_reject_private_172_16(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("172.16.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://internal.corp/callback")

    def test_reject_private_192_168(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.1.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://internal.corp/callback")

    def test_reject_ipv6_loopback(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::1", 0, 0, 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://[::1]/callback")

    def test_reject_link_local(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.1.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://link-local.example/callback")

    def test_dns_resolving_to_private(self):
        """Hostname that resolves to a private IP should be rejected."""
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.5", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://looks-public.example.com/hook")

    def test_allow_list_bypass(self):
        """IPs in allow_list should be accepted even if private."""
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            validate_webhook_url(
                "http://localhost:9001/callback",
                allow_list=["127.0.0.1"],
            )

    def test_allow_list_ipv6(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::1", 0, 0, 0)),
        ]):
            validate_webhook_url(
                "http://[::1]:9001/callback",
                allow_list=["::1"],
            )

    def test_dns_resolution_failure(self):
        import socket as sock_mod
        with patch("kiso.webhook.socket.getaddrinfo", side_effect=sock_mod.gaierror("no such host")):
            with pytest.raises(ValueError, match="Cannot resolve"):
                validate_webhook_url("http://nonexistent.invalid/hook")

    def test_no_hostname(self):
        with pytest.raises(ValueError, match="no hostname"):
            validate_webhook_url("http:///path")

    def test_multiple_ips_one_private_rejected(self):
        """If any resolved IP is private and not in allow_list, reject."""
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("http://dual.example.com/hook")

    def test_multiple_ips_all_public(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("93.184.216.35", 0)),
        ]):
            validate_webhook_url("http://multi.example.com/hook")


# --- deliver_webhook ---


class TestDeliverWebhook:
    async def test_successful_post(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            result = await deliver_webhook(
                "https://example.com/hook", "sess1", 42, "Hello!", True,
            )

        assert result is True
        mock_client.post.assert_called_once_with(
            "https://example.com/hook",
            json={
                "session": "sess1",
                "task_id": 42,
                "type": "msg",
                "content": "Hello!",
                "final": True,
            },
        )

    async def test_retry_on_500(self):
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200

        call_count = 0

        async def _post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_resp_500
            return mock_resp_200

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client), \
             patch("kiso.webhook.asyncio.sleep", new_callable=AsyncMock):
            result = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert result is True
        assert call_count == 2

    async def test_all_retries_fail(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client), \
             patch("kiso.webhook.asyncio.sleep", new_callable=AsyncMock):
            result = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert result is False
        assert mock_client.post.call_count == 3

    async def test_timeout_handling(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client), \
             patch("kiso.webhook.asyncio.sleep", new_callable=AsyncMock):
            result = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert result is False

    async def test_correct_payload_format(self):
        """Verify the exact payload structure."""
        captured_payload = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, json=None):
            captured_payload.update(json)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "my-session", 99, "Response text", False,
            )

        assert captured_payload == {
            "session": "my-session",
            "task_id": 99,
            "type": "msg",
            "content": "Response text",
            "final": False,
        }

    async def test_final_flag_true(self):
        captured_payload = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, json=None):
            captured_payload.update(json)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "Done", True,
            )

        assert captured_payload["final"] is True

    async def test_never_raises(self):
        """deliver_webhook should never raise, even on unexpected errors."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("unexpected"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client), \
             patch("kiso.webhook.asyncio.sleep", new_callable=AsyncMock):
            result = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert result is False
