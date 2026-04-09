"""Integration tests for in-flight message classification (M415)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from tests.conftest import AUTH_HEADER


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
    hints: list = []
    task = asyncio.create_task(_blocked_worker())
    _workers[session] = WorkerEntry(queue, task, cancel_event, pending, hints)
    _worker_phases[session] = WORKER_PHASE_EXECUTING
    return blocked, cancel_event, pending, hints, task


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


# ---------------------------------------------------------------------------
# M415-1: Stop fast-path — "STOP" → immediate cancel without LLM call
# ---------------------------------------------------------------------------


async def test_stop_fast_path_no_llm_call(client: httpx.AsyncClient):
    """Pure stop word triggers cancel without run_inflight_classifier LLM call."""
    sess = "inflight-stop-fp"
    blocked, cancel_event, _, _, task = _make_busy_worker(sess)
    classify_mock = AsyncMock(return_value="stop")
    try:
        with patch("kiso.main.run_inflight_classifier", classify_mock):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "STOP",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "stop"
        assert cancel_event.is_set()
        # run_inflight_classifier should NOT have been called (fast-path)
        classify_mock.assert_not_called()
    finally:
        await _cleanup_worker(sess, blocked, task)


async def test_stop_fast_path_variants(client: httpx.AsyncClient):
    """Various stop words trigger fast-path: ferma, cancel, abort, etc."""
    for idx, word in enumerate(["ferma", "CANCEL", "abort!", "basta", "quit"]):
        sess = f"inflight-stop-v{idx}"
        blocked, cancel_event, _, _, task = _make_busy_worker(sess)
        try:
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": word,
            }, headers=AUTH_HEADER)
            assert resp.status_code == 202
            assert resp.json()["inflight"] == "stop", f"Failed for: {word}"
            assert cancel_event.is_set(), f"Cancel not set for: {word}"
        finally:
            await _cleanup_worker(sess, blocked, task)


# ---------------------------------------------------------------------------
# M415-2: Independent classification → message queued, ack sent
# ---------------------------------------------------------------------------


async def test_independent_queues_to_pending_with_ack(client: httpx.AsyncClient):
    """Independent message → pending list, ack in response."""
    sess = "inflight-indep"
    blocked, _, pending, _, task = _make_busy_worker(sess)
    try:
        with patch("kiso.main.run_inflight_classifier", new_callable=AsyncMock, return_value="independent"):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "what time is it?",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "independent"
        assert "ack" in data
        assert len(pending) == 1
        assert pending[0]["content"] == "what time is it?"
    finally:
        await _cleanup_worker(sess, blocked, task)


# ---------------------------------------------------------------------------
# M415-3: Update classification → replan hint injected
# ---------------------------------------------------------------------------


async def test_update_adds_hint_with_ack(client: httpx.AsyncClient):
    """Update message → update_hints list, ack in response."""
    sess = "inflight-update"
    blocked, _, _, hints, task = _make_busy_worker(sess)
    try:
        with patch("kiso.main.run_inflight_classifier", new_callable=AsyncMock, return_value="update"):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "use port 8080 instead",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "update"
        assert "ack" in data
        assert len(hints) == 1
        assert hints[0] == "use port 8080 instead"
    finally:
        await _cleanup_worker(sess, blocked, task)


# ---------------------------------------------------------------------------
# M415-4: Conflict classification → current job cancelled, new job queued
# ---------------------------------------------------------------------------


async def test_conflict_cancels_and_queues_first(client: httpx.AsyncClient):
    """Conflict → cancel event set, new message at front of pending."""
    sess = "inflight-conflict"
    blocked, cancel_event, pending, _, task = _make_busy_worker(sess)
    try:
        with patch("kiso.main.run_inflight_classifier", new_callable=AsyncMock, return_value="conflict"):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "no, do Y instead",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        assert data["inflight"] == "conflict"
        assert "ack" in data
        assert cancel_event.is_set()
        assert len(pending) == 1
        assert pending[0]["content"] == "no, do Y instead"
    finally:
        await _cleanup_worker(sess, blocked, task)


# ---------------------------------------------------------------------------
# M415-5: Fast-path doesn't trigger on "stop using port 80"
# ---------------------------------------------------------------------------


async def test_stop_with_content_after_not_fast_path(client: httpx.AsyncClient):
    """'stop using port 80' is NOT a stop command — goes to LLM classifier."""
    sess = "inflight-not-stop"
    blocked, cancel_event, _, _, task = _make_busy_worker(sess)
    classify_mock = AsyncMock(return_value="update")
    try:
        with patch("kiso.main.run_inflight_classifier", classify_mock):
            resp = await client.post("/msg", json={
                "session": sess, "user": "testuser", "content": "stop using port 80",
            }, headers=AUTH_HEADER)
        assert resp.status_code == 202
        data = resp.json()
        # Should NOT be fast-path stop — classifier should have been called
        assert data["inflight"] == "update"
        classify_mock.assert_called_once()
        assert not cancel_event.is_set()
    finally:
        await _cleanup_worker(sess, blocked, task)


async def test_idle_worker_no_inflight(client: httpx.AsyncClient):
    """When worker is idle, no inflight handling — normal queue."""
    resp = await client.post("/msg", json={
        "session": "idle-sess", "user": "testuser", "content": "STOP",
    }, headers=AUTH_HEADER)
    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] is True
    assert "inflight" not in data
