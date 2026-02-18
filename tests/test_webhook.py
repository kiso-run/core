"""Tests for kiso/webhook.py — URL validation and delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
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

    def test_http_rejected_by_default(self):
        """require_https defaults to True, so http:// is rejected."""
        with pytest.raises(ValueError, match="must use https"):
            validate_webhook_url("http://example.com/callback")

    def test_http_allowed_when_require_https_false(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            validate_webhook_url("http://example.com/callback", require_https=False)

    def test_reject_ftp_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_webhook_url("ftp://example.com/file", require_https=False)

    def test_reject_no_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            validate_webhook_url("example.com/callback", require_https=False)

    def test_ftp_rejected_when_require_https_true(self):
        with pytest.raises(ValueError, match="must use https"):
            validate_webhook_url("ftp://example.com/file")

    def test_reject_loopback_127(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://localhost/callback")

    def test_reject_private_10(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://internal.corp/callback")

    def test_reject_private_172_16(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("172.16.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://internal.corp/callback")

    def test_reject_private_192_168(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.1.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://internal.corp/callback")

    def test_reject_ipv6_loopback(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::1", 0, 0, 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://[::1]/callback")

    def test_reject_link_local(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.1.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://link-local.example/callback")

    def test_dns_resolving_to_private(self):
        """Hostname that resolves to a private IP should be rejected."""
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.5", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://looks-public.example.com/hook")

    def test_allow_list_bypass(self):
        """IPs in allow_list should be accepted even if private."""
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            validate_webhook_url(
                "http://localhost:9001/callback",
                allow_list=["127.0.0.1"],
                require_https=False,
            )

    def test_allow_list_ipv6(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::1", 0, 0, 0)),
        ]):
            validate_webhook_url(
                "http://[::1]:9001/callback",
                allow_list=["::1"],
                require_https=False,
            )

    def test_dns_resolution_failure(self):
        import socket as sock_mod
        with patch("kiso.webhook.socket.getaddrinfo", side_effect=sock_mod.gaierror("no such host")):
            with pytest.raises(ValueError, match="Cannot resolve"):
                validate_webhook_url("https://nonexistent.invalid/hook")

    def test_no_hostname(self):
        with pytest.raises(ValueError, match="no hostname"):
            validate_webhook_url("http:///path", require_https=False)

    def test_multiple_ips_one_private_rejected(self):
        """If any resolved IP is private and not in allow_list, reject."""
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]):
            with pytest.raises(ValueError, match="private/reserved"):
                validate_webhook_url("https://dual.example.com/hook")

    def test_multiple_ips_all_public(self):
        with patch("kiso.webhook.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("93.184.216.35", 0)),
        ]):
            validate_webhook_url("https://multi.example.com/hook")


# --- No redirect ---


class TestWebhookNoRedirect:
    async def test_client_created_with_no_redirect(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client) as mock_cls:
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args[1]
        assert kwargs["follow_redirects"] is False


# --- deliver_webhook ---


class TestDeliverWebhook:
    async def test_successful_post(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        captured = {}

        async def _post(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            success, status_code, attempts = await deliver_webhook(
                "https://example.com/hook", "sess1", 42, "Hello!", True,
            )

        assert success is True
        assert status_code == 200
        assert attempts == 1
        assert captured["url"] == "https://example.com/hook"
        body = json.loads(captured["kwargs"]["content"])
        assert body == {
            "session": "sess1",
            "task_id": 42,
            "type": "msg",
            "content": "Hello!",
            "final": True,
        }

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
            success, status_code, attempts = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert success is True
        assert status_code == 200
        assert attempts == 2
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
            success, status_code, attempts = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert success is False
        assert status_code == 500
        assert attempts == 3
        assert mock_client.post.call_count == 3

    async def test_timeout_handling(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client), \
             patch("kiso.webhook.asyncio.sleep", new_callable=AsyncMock):
            success, status_code, attempts = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert success is False
        assert status_code == 0
        assert attempts == 3

    async def test_correct_payload_format(self):
        """Verify the exact payload structure."""
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "my-session", 99, "Response text", False,
            )

        body = json.loads(captured["content"])
        assert body == {
            "session": "my-session",
            "task_id": 99,
            "type": "msg",
            "content": "Response text",
            "final": False,
        }

    async def test_final_flag_true(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "Done", True,
            )

        body = json.loads(captured["content"])
        assert body["final"] is True

    async def test_never_raises(self):
        """deliver_webhook should never raise, even on unexpected errors."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("unexpected"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client), \
             patch("kiso.webhook.asyncio.sleep", new_callable=AsyncMock):
            success, status_code, attempts = await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert success is False
        assert attempts == 3


# --- HMAC signatures ---


class TestDeliverWebhookHMAC:
    async def test_hmac_signature_present(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
                secret="test-secret",
            )

        headers = captured["headers"]
        assert "X-Kiso-Signature" in headers
        assert headers["X-Kiso-Signature"].startswith("sha256=")

    async def test_hmac_signature_correct(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        secret = "my-webhook-secret"
        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "hello", False,
                secret=secret,
            )

        raw_body = captured["content"]
        expected_sig = hmac_mod.new(
            secret.encode(), raw_body, hashlib.sha256,
        ).hexdigest()
        assert captured["headers"]["X-Kiso-Signature"] == f"sha256={expected_sig}"

    async def test_no_signature_without_secret(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "text", False,
            )

        assert "X-Kiso-Signature" not in captured["headers"]


# --- Payload cap ---


class TestDeliverWebhookPayloadCap:
    async def test_content_truncated_over_max(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        long_content = "A" * 1000

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, long_content, False,
                max_payload=100,
            )

        body = json.loads(captured["content"])
        assert body["content"].endswith("[truncated]")
        assert len(body["content"]) < len(long_content)

    async def test_content_intact_under_max(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, "short", False,
                max_payload=1000,
            )

        body = json.loads(captured["content"])
        assert body["content"] == "short"

    async def test_no_truncation_when_zero(self):
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        long_content = "B" * 5000

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, long_content, False,
                max_payload=0,
            )

        body = json.loads(captured["content"])
        assert body["content"] == long_content


# --- Truncation safety ---


class TestTruncationSafety:
    async def test_truncation_respects_utf8_boundary(self):
        """Multi-byte content at cut point doesn't produce invalid UTF-8."""
        # 3-byte UTF-8 chars: each is 3 bytes encoded
        content = "\u4e16\u754c" * 100  # 600 bytes
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, content, False,
                max_payload=50,
            )

        body = json.loads(captured["content"])
        # Result should be valid UTF-8 and end with truncation marker
        assert body["content"].endswith("[truncated]")
        # Encoding the truncated content should not raise
        body["content"].encode("utf-8")

    async def test_truncation_marker_within_limit(self):
        """Final encoded content <= max_payload bytes."""
        content = "A" * 1000
        captured = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _post(url, **kwargs):
            captured.update(kwargs)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = _post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        max_payload = 100

        with patch("kiso.webhook.httpx.AsyncClient", return_value=mock_client):
            await deliver_webhook(
                "https://example.com/hook", "sess1", 1, content, False,
                max_payload=max_payload,
            )

        body = json.loads(captured["content"])
        # The content field (not the full JSON payload) should be within limit
        assert len(body["content"].encode()) <= max_payload
