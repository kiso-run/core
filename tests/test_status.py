"""Tests for GET /status/{session} endpoint."""

from __future__ import annotations

import httpx

from kiso.main import app
from kiso.store import save_message, create_session
from tests.conftest import AUTH_HEADER


async def test_empty_session_status(client: httpx.AsyncClient):
    resp = await client.get("/status/empty-sess", params={"user": "testadmin"}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"] == []
    assert data["plan"] is None
    assert data["queue_length"] == 0
    assert data["worker_running"] is False
    assert data["active_task"] is None


async def test_status_after_param(client: httpx.AsyncClient):
    """?after= parameter is accepted and returns 200."""
    resp = await client.get("/status/some-sess", params={"user": "testadmin", "after": 999}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


async def test_status_no_auth(client: httpx.AsyncClient):
    resp = await client.get("/status/test", params={"user": "testadmin"})
    assert resp.status_code == 401


async def test_status_missing_user_param_returns_422(client: httpx.AsyncClient):
    """Missing required ?user= query param returns 422 Unprocessable Entity."""
    resp = await client.get("/status/test", headers=AUTH_HEADER)
    assert resp.status_code == 422


# ── 90c: ownership check ───────────────────────────────────────────────────────


async def test_status_non_admin_forbidden_on_unowned_session(client: httpx.AsyncClient):
    """Non-admin user gets 403 when they have no messages in the session."""
    resp = await client.get(
        "/status/stranger-sess",
        params={"user": "testuser"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 403
    assert "Access denied" in resp.json()["detail"]


async def test_status_non_admin_allowed_on_owned_session(client: httpx.AsyncClient):
    """Non-admin user can read status of a session where they have posted messages."""
    db = app.state.db
    await create_session(db, "owned-by-testuser")
    await save_message(db, "owned-by-testuser", "testuser", "user", "hello", trusted=True, processed=True)

    resp = await client.get(
        "/status/owned-by-testuser",
        params={"user": "testuser"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200


async def test_status_admin_allowed_on_any_session(client: httpx.AsyncClient):
    """Admin user can read status of any session, including ones they don't own."""
    resp = await client.get(
        "/status/some-random-session",
        params={"user": "testadmin"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
