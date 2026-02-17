"""Tests for GET /health endpoint."""

from __future__ import annotations

import httpx


async def test_health_returns_ok(client: httpx.AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
