"""Tests for GET /sessions/{session}/info endpoint."""

from __future__ import annotations

import httpx

from tests.conftest import AUTH_HEADER


async def test_get_session_info(client: httpx.AsyncClient):
    """Endpoint returns message_count for a session with messages."""
    await client.post("/msg", json={
        "session": "info-sess",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    await client.post("/msg", json={
        "session": "info-sess",
        "user": "testuser",
        "content": "world",
    }, headers=AUTH_HEADER)

    resp = await client.get("/sessions/info-sess/info", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["session"] == "info-sess"
    assert data["message_count"] == 2
    assert data["summary"] is None


async def test_get_session_info_no_session(client: httpx.AsyncClient):
    """Non-existent session returns count 0 and no summary."""
    resp = await client.get("/sessions/nonexistent/info", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["session"] == "nonexistent"
    assert data["message_count"] == 0
    assert data["summary"] is None
