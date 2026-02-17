"""Tests for GET /status/{session} endpoint."""

from __future__ import annotations

import httpx

from tests.conftest import AUTH_HEADER


async def test_empty_session_status(client: httpx.AsyncClient):
    resp = await client.get("/status/empty-sess", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"] == []
    assert data["plan"] is None
    assert data["queue_length"] == 0
    assert data["worker_running"] is False
    assert data["active_task"] is None


async def test_status_after_param(client: httpx.AsyncClient):
    """?after= parameter is accepted and returns 200."""
    resp = await client.get("/status/some-sess", params={"after": 999}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


async def test_status_no_auth(client: httpx.AsyncClient):
    resp = await client.get("/status/test")
    assert resp.status_code == 401
