"""Tests for POST /admin/reload-env endpoint."""

from __future__ import annotations

import os
from unittest.mock import patch

import httpx

from kiso.config import KISO_DIR
from kiso.main import _ENV_VALUE_MAX_LEN, _rate_limiter
from tests.conftest import AUTH_HEADER, DISCORD_AUTH_HEADER


async def test_reload_env_as_admin(client: httpx.AsyncClient, tmp_path):
    """Admin user can reload .env and keys are loaded into os.environ."""
    env_file = KISO_DIR / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("KISO_TEST_VAR_A=hello\nKISO_TEST_VAR_B=world\n")

    try:
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reloaded"] is True
        assert data["keys_applied"] == 2
        assert os.environ["KISO_TEST_VAR_A"] == "hello"
        assert os.environ["KISO_TEST_VAR_B"] == "world"
    finally:
        os.environ.pop("KISO_TEST_VAR_A", None)
        os.environ.pop("KISO_TEST_VAR_B", None)
        env_file.unlink(missing_ok=True)


async def test_reload_env_as_user_forbidden(client: httpx.AsyncClient):
    """Non-admin user gets 403."""
    resp = await client.post(
        "/admin/reload-env",
        params={"user": "testuser"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["detail"]


async def test_reload_env_no_auth(client: httpx.AsyncClient):
    """Missing auth token gets 401."""
    resp = await client.post(
        "/admin/reload-env",
        params={"user": "testadmin"},
    )
    assert resp.status_code == 401


async def test_reload_env_missing_file(client: httpx.AsyncClient):
    """Missing .env file returns 200 with keys_applied: 0."""
    # Ensure .env doesn't exist
    env_file = KISO_DIR / ".env"
    existed = env_file.exists()
    if existed:
        backup = env_file.read_text()
        env_file.unlink()

    try:
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reloaded"] is True
        assert data["keys_applied"] == 0
    finally:
        if existed:
            env_file.write_text(backup)


async def test_reload_env_skips_comments_and_blanks(client: httpx.AsyncClient):
    """Comments and blank lines are skipped."""
    env_file = KISO_DIR / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "# This is a comment\n"
        "\n"
        "KISO_VALID_KEY=value1\n"
        "  # indented comment\n"
        "\n"
        "KISO_ANOTHER=value2\n"
    )

    try:
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["keys_applied"] == 2
        assert os.environ["KISO_VALID_KEY"] == "value1"
        assert os.environ["KISO_ANOTHER"] == "value2"
    finally:
        os.environ.pop("KISO_VALID_KEY", None)
        os.environ.pop("KISO_ANOTHER", None)
        env_file.unlink(missing_ok=True)


async def test_reload_env_strips_quotes(client: httpx.AsyncClient):
    """Surrounding double and single quotes are stripped from values."""
    env_file = KISO_DIR / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        'KISO_DQ="double quoted"\n'
        "KISO_SQ='single quoted'\n"
        "KISO_NQ=no quotes\n"
    )

    try:
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["keys_applied"] == 3
        assert os.environ["KISO_DQ"] == "double quoted"
        assert os.environ["KISO_SQ"] == "single quoted"
        assert os.environ["KISO_NQ"] == "no quotes"
    finally:
        os.environ.pop("KISO_DQ", None)
        os.environ.pop("KISO_SQ", None)
        os.environ.pop("KISO_NQ", None)
        env_file.unlink(missing_ok=True)


# ── 90b: allowlist / value validation ─────────────────────────────────────────


async def test_reload_env_skips_disallowed_prefix(client: httpx.AsyncClient):
    """Keys with unrecognised prefixes are counted in keys_skipped, not applied."""
    env_file = KISO_DIR / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "KISO_ALLOWED=keep\n"
        "PYTHONPATH=/evil\n"
        "LD_PRELOAD=/evil.so\n"
        "RANDOM_VAR=nope\n"
    )
    try:
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["keys_applied"] == 1
        assert data["keys_skipped"] == 3
        assert os.environ.get("KISO_ALLOWED") == "keep"
        assert "PYTHONPATH" not in os.environ or os.environ["PYTHONPATH"] != "/evil"
    finally:
        os.environ.pop("KISO_ALLOWED", None)
        env_file.unlink(missing_ok=True)


async def test_reload_env_skips_value_with_embedded_newline(client: httpx.AsyncClient):
    """Values containing \\r or \\n are rejected (defense-in-depth guard).

    The normal .env parser (splitlines) already strips newlines, so this guard
    is tested via mock to prove the validation logic fires independently.
    """
    injected = {"KISO_BAD": "value\rwith-cr", "KISO_GOOD": "ok"}
    with patch("kiso.main._load_env_file", return_value=injected):
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["keys_applied"] == 1
    assert data["keys_skipped"] == 1
    assert os.environ.get("KISO_GOOD") == "ok"
    os.environ.pop("KISO_GOOD", None)


async def test_reload_env_skips_value_exceeding_max_len(client: httpx.AsyncClient):
    """Values longer than _ENV_VALUE_MAX_LEN are skipped."""
    env_file = KISO_DIR / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    long_val = "x" * (_ENV_VALUE_MAX_LEN + 1)
    env_file.write_text(f"KISO_LONG={long_val}\nKISO_SHORT=ok\n")
    try:
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["keys_applied"] == 1
        assert data["keys_skipped"] == 1
        assert os.environ.get("KISO_SHORT") == "ok"
        assert "KISO_LONG" not in os.environ
    finally:
        os.environ.pop("KISO_SHORT", None)
        env_file.unlink(missing_ok=True)


async def test_reload_env_response_has_keys_skipped_field(client: httpx.AsyncClient):
    """Response always includes keys_skipped, even when zero."""
    env_file = KISO_DIR / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("KISO_X=1\n")
    try:
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        data = resp.json()
        assert "keys_skipped" in data
        assert data["keys_skipped"] == 0
    finally:
        os.environ.pop("KISO_X", None)
        env_file.unlink(missing_ok=True)


# ── 90a: admin endpoint rate limiting ─────────────────────────────────────────


async def test_reload_env_rate_limited_after_limit(client: httpx.AsyncClient):
    """POST /admin/reload-env returns 429 after 5 requests from the same admin user."""
    env_file = KISO_DIR / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("")

    # Exhaust the 5-request budget
    _rate_limiter.reset()
    try:
        for _ in range(5):
            r = await client.post(
                "/admin/reload-env",
                params={"user": "testadmin"},
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200

        # 6th request should be rate-limited
        resp = await client.post(
            "/admin/reload-env",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 429
        assert "Rate limit exceeded" in resp.json()["detail"]
    finally:
        env_file.unlink(missing_ok=True)
