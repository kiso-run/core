"""Tests for M26 — direct pub/ file serving (HMAC-based URLs)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.config import Config, KISO_DIR, Provider
from kiso.pub import pub_token, resolve_pub_token


# --- pub_token ---


class TestPubToken:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "test-secret-token"},
            providers={"p": Provider(base_url="http://x")},
            users={},
            models={},
            settings={},
            raw={},
        )

    def test_deterministic(self, config):
        t1 = pub_token("sess1", config)
        t2 = pub_token("sess1", config)
        assert t1 == t2

    def test_different_sessions(self, config):
        t1 = pub_token("sess1", config)
        t2 = pub_token("sess2", config)
        assert t1 != t2

    def test_length_is_16(self, config):
        t = pub_token("sess1", config)
        assert len(t) == 16

    def test_hex_chars_only(self, config):
        t = pub_token("sess1", config)
        assert all(c in "0123456789abcdef" for c in t)

    def test_raises_when_no_cli_token(self):
        cfg = Config(
            tokens={},
            providers={"p": Provider(base_url="http://x")},
            users={},
            models={},
            settings={},
            raw={},
        )
        with pytest.raises(ValueError, match="cli token not configured"):
            pub_token("sess1", cfg)


# --- resolve_pub_token ---


class TestResolvePubToken:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "test-secret-token"},
            providers={"p": Provider(base_url="http://x")},
            users={},
            models={},
            settings={},
            raw={},
        )

    def test_finds_session(self, tmp_path, config):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "my-session").mkdir()

        with patch("kiso.pub.KISO_DIR", tmp_path):
            token = pub_token("my-session", config)
            result = resolve_pub_token(token, config)
        assert result == "my-session"

    def test_no_match(self, tmp_path, config):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "my-session").mkdir()

        with patch("kiso.pub.KISO_DIR", tmp_path):
            result = resolve_pub_token("0000000000000000", config)
        assert result is None

    def test_no_sessions_dir(self, tmp_path, config):
        with patch("kiso.pub.KISO_DIR", tmp_path):
            result = resolve_pub_token("anything", config)
        assert result is None


# --- GET /pub/{token}/{filename} endpoint ---


class TestGetPubEndpoint:
    @pytest.fixture()
    def _setup_session(self, client, tmp_path):
        """Create a session directory with pub/ inside tmp_path for endpoint tests."""
        config = client._transport.app.state.config  # type: ignore[attr-defined]
        self._config = config
        self._session = "test-pub-session"
        self._sessions_dir = tmp_path / "sessions"
        self._session_dir = self._sessions_dir / self._session
        self._pub_dir = self._session_dir / "pub"
        self._pub_dir.mkdir(parents=True, exist_ok=True)
        self._token = pub_token(self._session, config)
        # Patch KISO_DIR in both modules so token resolution and file serving use tmp_path
        with patch("kiso.main.KISO_DIR", tmp_path), \
             patch("kiso.pub.KISO_DIR", tmp_path):
            yield

    async def test_serves_file(self, client, _setup_session):
        test_file = self._pub_dir / "hello.txt"
        test_file.write_text("Hello, world!")

        resp = await client.get(f"/pub/{self._token}/hello.txt")
        assert resp.status_code == 200
        assert resp.text == "Hello, world!"
        assert "hello.txt" in resp.headers.get("content-disposition", "")

    async def test_404_missing_file(self, client, _setup_session):
        resp = await client.get(f"/pub/{self._token}/nonexistent.txt")
        assert resp.status_code == 404

    async def test_404_bad_token(self, client, _setup_session):
        resp = await client.get("/pub/0000000000000000/hello.txt")
        assert resp.status_code == 404

    async def test_path_traversal_blocked(self, client, _setup_session):
        resp = await client.get(f"/pub/{self._token}/../../etc/passwd")
        assert resp.status_code == 404

    async def test_path_traversal_sibling_dir_blocked(self, client, _setup_session):
        """Sibling directory with same-prefix name must not be accessible.

        /sessions/test-pub-session/pub-evil/secret starts with
        /sessions/test-pub-session/pub — the old startswith() check would
        pass this; is_relative_to() correctly rejects it.
        """
        sibling = self._session_dir / "pub-evil"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("should not be served")

        resp = await client.get(f"/pub/{self._token}/../pub-evil/secret.txt")
        assert resp.status_code == 404

    async def test_preserves_extension(self, client, _setup_session):
        test_file = self._pub_dir / "report.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake")

        resp = await client.get(f"/pub/{self._token}/report.pdf")
        assert resp.status_code == 200
        assert "pdf" in resp.headers.get("content-type", "")

    async def test_unknown_extension_octet_stream(self, client, _setup_session):
        test_file = self._pub_dir / "data.xyz123"
        test_file.write_bytes(b"\x00\x01\x02")

        resp = await client.get(f"/pub/{self._token}/data.xyz123")
        assert resp.status_code == 200
        assert "octet-stream" in resp.headers.get("content-type", "")

    async def test_nested_subdirectory(self, client, _setup_session):
        sub_dir = self._pub_dir / "sub"
        sub_dir.mkdir()
        test_file = sub_dir / "file.txt"
        test_file.write_text("nested content")

        resp = await client.get(f"/pub/{self._token}/sub/file.txt")
        assert resp.status_code == 200
        assert resp.text == "nested content"

    async def test_no_auth_required(self, client, _setup_session):
        test_file = self._pub_dir / "open.txt"
        test_file.write_text("public")

        resp = await client.get(f"/pub/{self._token}/open.txt", headers={})
        assert resp.status_code == 200
