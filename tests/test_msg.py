"""Tests for POST /msg endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

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
    status_resp = await client.get("/status/new-sess", params={"user": "testadmin"}, headers=AUTH_HEADER)
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
        entry.task.cancel()
        try:
            await entry.task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# — In-flight message handling
# ---------------------------------------------------------------------------


def _make_busy_worker(session: str):
    """Set up a blocked worker + busy phase so inflight detection triggers."""
    from kiso.main import _workers, _worker_phases, WorkerEntry
    from kiso.brain import WORKER_PHASE_EXECUTING

    blocked = asyncio.Event()

    async def _blocked_worker(*args, **kwargs):
        await blocked.wait()

    queue = asyncio.Queue(maxsize=10)
    cancel_event = asyncio.Event()
    pending: list = []
    task = asyncio.create_task(_blocked_worker())
    _workers[session] = WorkerEntry(queue, task, cancel_event, pending)
    _worker_phases[session] = WORKER_PHASE_EXECUTING
    return blocked, cancel_event, pending, task


async def _cleanup_worker(session: str, blocked, task):
    from kiso.main import _workers, _worker_phases
    blocked.set()
    _workers.pop(session, None)
    _worker_phases.pop(session, None)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_inflight_stop_fast_path(client: httpx.AsyncClient):
    """stop message during active job triggers fast-path cancel."""
    sess = "inflight-stop-test"
    blocked, cancel_event, _, task = _make_busy_worker(sess)
    try:
        resp = await client.post("/msg", json={
            "session": sess, "user": "testuser", "content": "STOP",
        }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "stop"
        assert cancel_event.is_set()
    finally:
        await _cleanup_worker(sess, blocked, task)


async def test_inflight_stop_via_classifier(client: httpx.AsyncClient):
    """LLM classifier returns 'stop' → cancel event set."""
    sess = "inflight-llm-stop"
    blocked, cancel_event, _, task = _make_busy_worker(sess)
    try:
        with patch("kiso.main.classify_inflight", new_callable=AsyncMock, return_value="stop"):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "annulla tutto per favore",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "stop"
        assert cancel_event.is_set()
    finally:
        await _cleanup_worker(sess, blocked, task)


async def test_inflight_independent_queued_to_pending(client: httpx.AsyncClient):
    """independent message goes to pending_messages with ack."""
    sess = "inflight-independent"
    blocked, _, pending, task = _make_busy_worker(sess)
    try:
        with patch("kiso.main.classify_inflight", new_callable=AsyncMock, return_value="independent"):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "che ore sono?",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "independent"
        assert "ack" in data
        assert len(pending) == 1
        assert pending[0]["content"] == "che ore sono?"
    finally:
        await _cleanup_worker(sess, blocked, task)


async def test_inflight_update_adds_hint(client: httpx.AsyncClient):
    """update message adds to update_hints with ack."""
    sess = "inflight-update"
    blocked, _, _, task = _make_busy_worker(sess)
    try:
        from kiso.main import _workers
        entry = _workers[sess]
        with patch("kiso.main.classify_inflight", new_callable=AsyncMock, return_value="update"):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "usa porta 8080",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "update"
        assert "ack" in data
        assert len(entry.update_hints) == 1
        assert entry.update_hints[0] == "usa porta 8080"
    finally:
        await _cleanup_worker(sess, blocked, task)


async def test_inflight_conflict_cancels_and_queues(client: httpx.AsyncClient):
    """conflict cancels current job and queues new message first."""
    sess = "inflight-conflict"
    blocked, cancel_event, pending, task = _make_busy_worker(sess)
    try:
        with patch("kiso.main.classify_inflight", new_callable=AsyncMock, return_value="conflict"):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "no fai X invece",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "conflict"
        assert "ack" in data
        assert cancel_event.is_set()
        # New message is at the front of pending
        assert len(pending) == 1
        assert pending[0]["content"] == "no fai X invece"
    finally:
        await _cleanup_worker(sess, blocked, task)


async def test_idle_worker_skips_inflight(client: httpx.AsyncClient):
    """When worker is idle, stop messages go through normal queue (no fast-path)."""
    resp = await client.post("/msg", json={
        "session": "idle-test", "user": "testuser", "content": "STOP",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] is True
    assert "inflight" not in data


# ---------------------------------------------------------------------------
# — Safety rules API endpoints
# ---------------------------------------------------------------------------

ADMIN_HEADER = AUTH_HEADER  # testadmin token in conftest


async def test_safety_rules_crud(client: httpx.AsyncClient):
    """add → list → remove safety rules via API."""
    # List — initially empty
    resp = await client.get("/safety-rules", headers=ADMIN_HEADER)
    assert resp.status_code == 200
    assert resp.json()["rules"] == []

    # Add a rule
    resp = await client.post("/safety-rules", json={"content": "Never delete /data"},
                             headers=ADMIN_HEADER)
    assert resp.status_code == 201
    rule_id = resp.json()["id"]
    assert resp.json()["content"] == "Never delete /data"

    # List — now has one rule
    resp = await client.get("/safety-rules", headers=ADMIN_HEADER)
    assert len(resp.json()["rules"]) == 1
    assert resp.json()["rules"][0]["content"] == "Never delete /data"

    # Remove the rule
    resp = await client.delete(f"/safety-rules/{rule_id}", headers=ADMIN_HEADER)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # List — empty again
    resp = await client.get("/safety-rules", headers=ADMIN_HEADER)
    assert resp.json()["rules"] == []


async def test_safety_rule_empty_content_rejected(client: httpx.AsyncClient):
    """empty content rejected with 400."""
    resp = await client.post("/safety-rules", json={"content": "   "},
                             headers=ADMIN_HEADER)
    assert resp.status_code == 400


async def test_safety_rule_delete_nonexistent(client: httpx.AsyncClient):
    """deleting nonexistent rule returns 404."""
    resp = await client.delete("/safety-rules/99999", headers=ADMIN_HEADER)
    assert resp.status_code == 404
