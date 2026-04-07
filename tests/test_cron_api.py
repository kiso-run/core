"""Integration tests for /cron API endpoints."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import AUTH_HEADER, DISCORD_AUTH_HEADER


async def test_list_cron_empty(client: httpx.AsyncClient):
    resp = await client.get("/cron", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["jobs"] == []


async def test_create_cron_job(client: httpx.AsyncClient):
    resp = await client.post("/cron", headers=AUTH_HEADER, json={
        "session": "cron-test",
        "schedule": "0 9 * * *",
        "prompt": "check competitor prices",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["schedule"] == "0 9 * * *"
    assert "next_run" in data


async def test_list_cron_after_create(client: httpx.AsyncClient):
    await client.post("/cron", headers=AUTH_HEADER, json={
        "session": "cron-test", "schedule": "*/5 * * * *", "prompt": "ping",
    })
    resp = await client.get("/cron", headers=AUTH_HEADER)
    jobs = resp.json()["jobs"]
    assert len(jobs) >= 1
    assert any(j["prompt"] == "ping" for j in jobs)


async def test_list_cron_filter_by_session(client: httpx.AsyncClient):
    await client.post("/cron", headers=AUTH_HEADER, json={
        "session": "sessA", "schedule": "0 9 * * *", "prompt": "job A",
    })
    await client.post("/cron", headers=AUTH_HEADER, json={
        "session": "sessB", "schedule": "0 9 * * *", "prompt": "job B",
    })
    resp = await client.get("/cron", headers=AUTH_HEADER, params={"session": "sessA"})
    jobs = resp.json()["jobs"]
    assert all(j["session"] == "sessA" for j in jobs)


async def test_create_cron_invalid_schedule(client: httpx.AsyncClient):
    resp = await client.post("/cron", headers=AUTH_HEADER, json={
        "session": "test", "schedule": "not a cron", "prompt": "hi",
    })
    assert resp.status_code == 400
    assert "Invalid cron" in resp.json()["detail"]


async def test_create_cron_non_admin_rejected(client: httpx.AsyncClient):
    resp = await client.post("/cron", headers=DISCORD_AUTH_HEADER, json={
        "session": "test", "schedule": "0 9 * * *", "prompt": "hi",
    })
    assert resp.status_code == 403


async def test_delete_cron_job(client: httpx.AsyncClient):
    resp = await client.post("/cron", headers=AUTH_HEADER, json={
        "session": "del-test", "schedule": "0 9 * * *", "prompt": "to delete",
    })
    job_id = resp.json()["id"]
    resp = await client.delete(f"/cron/{job_id}", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


async def test_delete_cron_not_found(client: httpx.AsyncClient):
    resp = await client.delete("/cron/99999", headers=AUTH_HEADER)
    assert resp.status_code == 404


async def test_patch_cron_enable_disable(client: httpx.AsyncClient):
    resp = await client.post("/cron", headers=AUTH_HEADER, json={
        "session": "toggle-test", "schedule": "0 9 * * *", "prompt": "toggle",
    })
    job_id = resp.json()["id"]

    # Disable
    resp = await client.patch(f"/cron/{job_id}", headers=AUTH_HEADER, params={"enabled": "false"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    # Enable
    resp = await client.patch(f"/cron/{job_id}", headers=AUTH_HEADER, params={"enabled": "true"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
