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


async def test_health_disk_used_is_kiso_dir_size(client: httpx.AsyncClient):
    """M219 fix: disk_gb.used should reflect KISO_DIR size, not whole filesystem."""
    resp = await client.get("/health")
    data = resp.json()
    disk = data["resources"]["disk_gb"]
    # disk_used should be reasonable (< a few GB for test env)
    # and definitely less than disk_total (the whole filesystem)
    if disk["used"] is not None and disk["limit"] is not None:
        assert disk["used"] < disk["limit"], (
            f"disk used ({disk['used']} GB) >= total ({disk['limit']} GB) — "
            f"likely measuring whole filesystem instead of KISO_DIR"
        )


async def test_health_disk_limit_from_config(client: httpx.AsyncClient):
    """M231: disk_gb.limit should use max_disk_gb from config, not filesystem total."""
    from kiso.main import app
    max_disk = app.state.config.settings.get("max_disk_gb")
    resp = await client.get("/health")
    data = resp.json()
    disk_limit = data["resources"]["disk_gb"]["limit"]
    if max_disk is not None:
        assert disk_limit == max_disk, (
            f"disk limit ({disk_limit}) should match config max_disk_gb ({max_disk})"
        )
