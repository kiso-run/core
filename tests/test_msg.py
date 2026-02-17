"""Tests for POST /msg endpoint."""

from __future__ import annotations

import httpx

from tests.conftest import AUTH_HEADER, DISCORD_AUTH_HEADER


async def test_trusted_msg_returns_202_queued(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "test-sess",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] is True
    assert data["session"] == "test-sess"


async def test_unknown_user_returns_202_untrusted(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "test-sess",
        "user": "stranger",
        "content": "hello",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 202
    data = resp.json()
    assert data["untrusted"] is True


async def test_invalid_session_returns_400(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "bad session!!!",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 400


async def test_no_auth_returns_401(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "test-sess",
        "user": "testuser",
        "content": "hello",
    })
    assert resp.status_code == 401


async def test_implicit_session_create(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "new-sess",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 202
    # session should now be accessible via /status
    status_resp = await client.get("/status/new-sess", headers=AUTH_HEADER)
    assert status_resp.status_code == 200


async def test_alias_resolution(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "test-sess",
        "user": "TestUser#1234",
        "content": "hello from discord",
    }, headers=DISCORD_AUTH_HEADER)
    assert resp.status_code == 202
    assert resp.json()["queued"] is True


async def test_missing_fields_returns_422(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "test-sess",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 422


async def test_session_id_max_length_valid(client: httpx.AsyncClient):
    long_id = "a" * 255
    resp = await client.post("/msg", json={
        "session": long_id,
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 202


async def test_session_id_too_long_returns_400(client: httpx.AsyncClient):
    long_id = "a" * 256
    resp = await client.post("/msg", json={
        "session": long_id,
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 400


async def test_empty_session_id_returns_400(client: httpx.AsyncClient):
    resp = await client.post("/msg", json={
        "session": "",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 400
