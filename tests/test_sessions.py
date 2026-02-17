"""Tests for GET /sessions and POST /sessions endpoints."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from tests.conftest import AUTH_HEADER


async def test_user_sees_own_sessions(client: httpx.AsyncClient):
    # Create a message so session exists with user's messages
    await client.post("/msg", json={
        "session": "my-sess",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)

    resp = await client.get("/sessions", params={"user": "testuser"}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 1
    assert sessions[0]["session"] == "my-sess"


async def test_admin_all_sees_all(client: httpx.AsyncClient):
    # Create messages in different sessions by different users
    await client.post("/msg", json={
        "session": "sess-a",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    await client.post("/msg", json={
        "session": "sess-b",
        "user": "testadmin",
        "content": "hello",
    }, headers=AUTH_HEADER)

    resp = await client.get(
        "/sessions",
        params={"user": "testadmin", "all": "true"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 2


async def test_non_admin_all_sees_only_own(client: httpx.AsyncClient):
    """Non-admin user with all=true should only see their own sessions."""
    await client.post("/msg", json={
        "session": "sess-x",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    await client.post("/msg", json={
        "session": "sess-y",
        "user": "testadmin",
        "content": "hello",
    }, headers=AUTH_HEADER)

    resp = await client.get(
        "/sessions",
        params={"user": "testuser", "all": "true"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    sessions = resp.json()
    # testuser is not admin, so all=true is ignored
    assert len(sessions) == 1
    assert sessions[0]["session"] == "sess-x"


async def test_sessions_missing_user_param(client: httpx.AsyncClient):
    resp = await client.get("/sessions", headers=AUTH_HEADER)
    assert resp.status_code == 422


async def test_sessions_no_auth(client: httpx.AsyncClient):
    resp = await client.get("/sessions", params={"user": "testuser"})
    assert resp.status_code == 401


# --- POST /sessions ---


async def test_post_create_new_session(client: httpx.AsyncClient):
    resp = await client.post("/sessions", json={
        "session": "new-sess",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 201
    data = resp.json()
    assert data["session"] == "new-sess"
    assert data["created"] is True


async def test_post_update_existing_session(client: httpx.AsyncClient):
    # Create first
    resp1 = await client.post("/sessions", json={
        "session": "upd-sess",
        "description": "first",
    }, headers=AUTH_HEADER)
    assert resp1.status_code == 201
    assert resp1.json()["created"] is True

    # Update
    resp2 = await client.post("/sessions", json={
        "session": "upd-sess",
        "description": "second",
    }, headers=AUTH_HEADER)
    assert resp2.status_code == 200
    assert resp2.json()["created"] is False


async def test_post_with_webhook_validates_and_stores(client: httpx.AsyncClient):
    with patch("kiso.main.validate_webhook_url"):
        resp = await client.post("/sessions", json={
            "session": "webhook-sess",
            "webhook": "https://example.com/hook",
        }, headers=AUTH_HEADER)
    assert resp.status_code == 201

    # Verify webhook is stored by checking the DB
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    from kiso.store import get_session
    sess = await get_session(db, "webhook-sess")
    assert sess is not None
    assert sess["webhook"] == "https://example.com/hook"


async def test_post_with_invalid_webhook_private_ip(client: httpx.AsyncClient):
    with patch("kiso.main.validate_webhook_url", side_effect=ValueError("private/reserved")):
        resp = await client.post("/sessions", json={
            "session": "bad-hook",
            "webhook": "http://10.0.0.1/hook",
        }, headers=AUTH_HEADER)
    assert resp.status_code == 400
    assert "private/reserved" in resp.json()["detail"]


async def test_post_webhook_allow_list(client: httpx.AsyncClient):
    """Webhook on allow_list should be accepted."""
    with patch("kiso.main.validate_webhook_url"):
        resp = await client.post("/sessions", json={
            "session": "allowed-hook",
            "webhook": "http://localhost:9001/callback",
        }, headers=AUTH_HEADER)
    assert resp.status_code == 201


async def test_post_invalid_session_id(client: httpx.AsyncClient):
    resp = await client.post("/sessions", json={
        "session": "bad session!!!",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 400
    assert "Invalid session ID" in resp.json()["detail"]


async def test_post_sessions_no_auth(client: httpx.AsyncClient):
    resp = await client.post("/sessions", json={
        "session": "no-auth",
    })
    assert resp.status_code == 401
