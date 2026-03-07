"""Tests for GET /health endpoint."""

from __future__ import annotations

import httpx


async def test_health_returns_ok(client: httpx.AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "build_hash" in data


async def test_health_includes_version(client: httpx.AsyncClient):
    resp = await client.get("/health")
    data = resp.json()
    from kiso._version import __version__
    assert data["version"] == __version__
    # Default build_hash when KISO_BUILD_HASH env var is not set
    assert isinstance(data["build_hash"], str)


async def test_health_includes_resources(client: httpx.AsyncClient):
    """M219: /health response contains resources dict with expected keys."""
    resp = await client.get("/health")
    data = resp.json()
    assert "resources" in data
    res = data["resources"]
    assert "memory_mb" in res
    assert "used" in res["memory_mb"] and "limit" in res["memory_mb"]
    assert "cpu" in res
    assert "limit" in res["cpu"]
    assert "disk_gb" in res
    assert "used" in res["disk_gb"] and "limit" in res["disk_gb"]
    assert "pids" in res
    assert "used" in res["pids"] and "limit" in res["pids"]
