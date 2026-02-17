"""Tests for POST /admin/reload-env endpoint."""

from __future__ import annotations

import os
from unittest.mock import patch

import httpx

from kiso.config import KISO_DIR
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
        assert data["keys_loaded"] == 2
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
    """Missing .env file returns 200 with keys_loaded: 0."""
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
        assert data["keys_loaded"] == 0
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
        assert resp.json()["keys_loaded"] == 2
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
        assert resp.json()["keys_loaded"] == 3
        assert os.environ["KISO_DQ"] == "double quoted"
        assert os.environ["KISO_SQ"] == "single quoted"
        assert os.environ["KISO_NQ"] == "no quotes"
    finally:
        os.environ.pop("KISO_DQ", None)
        os.environ.pop("KISO_SQ", None)
        os.environ.pop("KISO_NQ", None)
        env_file.unlink(missing_ok=True)
