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


async def test_msg_content_too_large_returns_413(client: httpx.AsyncClient):
    """100KB content exceeds default max_message_size (64KB) → 413."""
    large_content = "x" * 100_000
    resp = await client.post("/msg", json={
        "session": "test-sess",
        "user": "testuser",
        "content": large_content,
    }, headers=AUTH_HEADER)
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


async def test_msg_content_at_limit_accepted(client: httpx.AsyncClient):
    """Content exactly at max_message_size (64KB) is accepted."""
    content = "x" * 65536
    resp = await client.post("/msg", json={
        "session": "test-sess",
        "user": "testuser",
        "content": content,
    }, headers=AUTH_HEADER)
    assert resp.status_code == 202


async def test_msg_content_one_over_limit_rejected(client: httpx.AsyncClient):
    """Content one byte over max_message_size is rejected."""
    content = "x" * 65537
    resp = await client.post("/msg", json={
        "session": "test-sess",
        "user": "testuser",
        "content": content,
    }, headers=AUTH_HEADER)
    assert resp.status_code == 413


async def test_msg_queue_full_returns_429(client: httpx.AsyncClient):
    """Pre-fill queue to capacity, verify next message returns 429."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from kiso.main import _workers

    # Set max_queue_size=1 so queue fills after one item
    cfg = client._transport.app.state.config
    client._transport.app.state.config = cfg.__class__(
        tokens=cfg.tokens,
        providers=cfg.providers,
        users=cfg.users,
        models=cfg.models,
        settings={**cfg.settings, "max_queue_size": 1},
        raw=cfg.raw,
    )

    # Block the worker so it never drains the queue
    blocked = asyncio.Event()

    async def _blocked_worker(*args, **kwargs):
        await blocked.wait()

    with patch("kiso.main.run_worker", _blocked_worker):
        # First message — creates worker and fills the queue (size=1)
        resp1 = await client.post("/msg", json={
            "session": "queue-test",
            "user": "testuser",
            "content": "first",
        }, headers=AUTH_HEADER)
        assert resp1.status_code == 202

        # Second message — queue is full → 429
        resp2 = await client.post("/msg", json={
            "session": "queue-test",
            "user": "testuser",
            "content": "second",
        }, headers=AUTH_HEADER)
        assert resp2.status_code == 429

    # Clean up: unblock and cancel the worker task
    blocked.set()
    entry = _workers.pop("queue-test", None)
    if entry:
        entry[1].cancel()
        try:
            await entry[1]
        except asyncio.CancelledError:
            pass
