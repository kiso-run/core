"""Tests for GET /sessions endpoint."""

from __future__ import annotations

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
