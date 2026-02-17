"""Tests for kiso/auth.py."""

from __future__ import annotations

import httpx

from kiso.auth import resolve_user
from kiso.config import Config

from tests.conftest import AUTH_HEADER


def test_resolve_direct_username(test_config: Config):
    result = resolve_user(test_config, "testuser", "cli")
    assert result.username == "testuser"
    assert result.trusted is True


def test_resolve_alias(test_config: Config):
    result = resolve_user(test_config, "TestUser#1234", "discord")
    assert result.username == "testuser"
    assert result.trusted is True


def test_resolve_unknown_untrusted(test_config: Config):
    result = resolve_user(test_config, "stranger", "cli")
    assert result.username == "stranger"
    assert result.trusted is False
    assert result.user is None


def test_resolve_alias_wrong_connector(test_config: Config):
    """Alias only matches when connector (token_name) matches."""
    result = resolve_user(test_config, "TestUser#1234", "cli")
    assert result.trusted is False


async def test_require_auth_valid(client: httpx.AsyncClient):
    resp = await client.get("/status/test", headers=AUTH_HEADER)
    assert resp.status_code == 200


async def test_require_auth_missing(client: httpx.AsyncClient):
    resp = await client.get("/status/test")
    assert resp.status_code == 401


async def test_require_auth_invalid(client: httpx.AsyncClient):
    resp = await client.get("/status/test", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
