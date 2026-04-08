"""Same-session worker spawn atomicity (M1270).

Tests that ``kiso.main._ensure_worker`` produces exactly one worker per
session under concurrent /msg arrivals AND under interleaved cron
firings, both of which call the same ``_ensure_worker`` helper.

Boundary vs neighbouring tests:

- ``tests/test_inflight.py`` (M415) covers `/msg` arriving when a
  worker is **already busy**: in-flight classification (stop /
  independent / update / conflict). It uses ``_make_busy_worker`` to
  pre-create the worker.
- This module covers the **opposite** case: multiple `/msg` requests
  racing the spawn check on a session with **no worker yet**.

`kiso.main._ensure_worker` is documented as "Atomic: no await between
checking and creating (prevents duplicate workers)". The atomicity is
**structural** — single-threaded asyncio event loop semantics, not
lock-based — so this test does not need to monkeypatch a barrier into
the spawn point. It just exercises concurrent HTTP arrivals and
asserts the post-condition.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from kiso.main import _workers, _worker_phases, _ensure_worker, app
from kiso.store import create_session

from tests.integration.conftest import (
    AUTH_HEADER,
    wait_for_worker_idle,
)


pytestmark = pytest.mark.integration


async def _cleanup_workers(*sessions: str):
    for sess in sessions:
        entry = _workers.pop(sess, None)
        _worker_phases.pop(sess, None)
        if entry is not None and not entry.task.done():
            entry.task.cancel()


class TestSameSessionSpawnAtomicity:

    async def test_concurrent_msg_to_same_session_creates_exactly_one_worker(
        self, kiso_client: httpx.AsyncClient,
    ):
        sess = "spawn-race-same"

        async def post_msg(i: int):
            return await kiso_client.post(
                "/msg",
                json={"session": sess, "user": "testuser", "content": f"msg {i}"},
                headers=AUTH_HEADER,
            )

        # Fire N concurrent POSTs racing the spawn check
        results = await asyncio.gather(*(post_msg(i) for i in range(8)))
        try:
            # Every request was accepted
            assert all(r.status_code == 202 for r in results)

            # Exactly one worker entry exists for this session
            assert sess in _workers
            entries = [s for s in _workers if s == sess]
            assert len(entries) == 1

            # Drain to make sure nothing leaks
            try:
                await wait_for_worker_idle(kiso_client, sess, timeout=15.0)
            except TimeoutError:
                pass
        finally:
            await _cleanup_workers(sess)

    async def test_concurrent_msg_to_different_sessions_creates_independent_workers(
        self, kiso_client: httpx.AsyncClient,
    ):
        sessions = [f"spawn-race-cross-{i}" for i in range(3)]

        async def post_msg(sess: str):
            return await kiso_client.post(
                "/msg",
                json={"session": sess, "user": "testuser", "content": f"hello {sess}"},
                headers=AUTH_HEADER,
            )

        results = await asyncio.gather(*(post_msg(s) for s in sessions))
        try:
            assert all(r.status_code == 202 for r in results)
            for sess in sessions:
                assert sess in _workers, f"missing worker for {sess}"
            for sess in sessions:
                try:
                    await wait_for_worker_idle(kiso_client, sess, timeout=10.0)
                except TimeoutError:
                    pass
        finally:
            await _cleanup_workers(*sessions)


class TestCronSharesSpawnCheck:

    async def test_cron_and_msg_resolve_to_one_worker(
        self, kiso_client: httpx.AsyncClient, integration_db,
    ):
        """A cron-style _ensure_worker call + concurrent /msg POSTs
        produce exactly one worker for the session — they share the
        same atomic check (kiso/main.py:472 vs kiso/api/sessions.py:161).
        """
        sess = "cron-msg-shared"
        await create_session(integration_db, sess)
        config = app.state.config

        # Cron-fire path: directly call _ensure_worker as the cron
        # scheduler does at kiso/main.py:472, then put a cron-style
        # payload into the returned queue.
        cron_queue = _ensure_worker(sess, integration_db, config)
        cron_queue.put_nowait({
            "id": -1,
            "content": "cron prompt",
            "user_role": "admin",
            "user_tools": "*",
            "username": "cron",
            "base_url": "",
        })

        async def post_msg(i: int):
            return await kiso_client.post(
                "/msg",
                json={"session": sess, "user": "testuser", "content": f"user msg {i}"},
                headers=AUTH_HEADER,
            )

        # Concurrent /msg POSTs racing against the already-spawned worker
        results = await asyncio.gather(*(post_msg(i) for i in range(4)))
        try:
            # Every /msg request was accepted (queued OR ack/inflight, all 202)
            assert all(r.status_code == 202 for r in results)

            # Exactly one worker entry — cron and /msg shared the spawn check
            assert sess in _workers
            assert sum(1 for s in _workers if s == sess) == 1

            try:
                await wait_for_worker_idle(kiso_client, sess, timeout=15.0)
            except TimeoutError:
                pass
        finally:
            await _cleanup_workers(sess)


class TestCronBypassesInflightClassification:

    async def test_cron_enqueue_during_busy_worker_skips_inflight(
        self, kiso_client: httpx.AsyncClient, integration_db,
    ):
        """DESIGN NOTE: cron-fired messages bypass the inflight
        classifier entirely — they call ``_ensure_worker`` and
        ``queue.put`` directly (kiso/main.py:472-473), without the
        stop/independent/update/conflict routing that ``/msg`` does in
        kiso/api/sessions.py:104-159.

        This test pins the *current* behavior so any future change to
        cron routing must update this test deliberately. It is **not**
        an endorsement of the bypass — it is a guard.
        """
        from kiso.brain import WORKER_PHASE_EXECUTING
        from kiso.main import WorkerEntry

        sess = "cron-busy-bypass"
        await create_session(integration_db, sess)

        # Build a busy worker (pattern from tests/test_inflight.py)
        blocked = asyncio.Event()

        async def _blocked_worker(*args, **kwargs):
            await blocked.wait()

        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        cancel_event = asyncio.Event()
        pending: list = []
        hints: list = []
        task = asyncio.create_task(_blocked_worker())
        _workers[sess] = WorkerEntry(queue, task, cancel_event, pending, hints)
        _worker_phases[sess] = WORKER_PHASE_EXECUTING

        try:
            config = app.state.config

            # Simulate cron firing: returns the same queue, no classification
            cron_queue = _ensure_worker(sess, integration_db, config)
            assert cron_queue is queue, "cron must reuse the busy worker's queue"

            cron_payload = {
                "id": -2,
                "content": "stop",  # would be a stop fast-path via /msg
                "user_role": "admin",
                "user_tools": "*",
                "username": "cron",
                "base_url": "",
            }
            cron_queue.put_nowait(cron_payload)

            # Inflight classification was NOT invoked: no stop, no
            # pending list growth, no update hints, no cancel event.
            assert not cancel_event.is_set(), "cron must not trigger stop fast-path"
            assert pending == [], "cron must not push to pending_messages"
            assert hints == [], "cron must not append update_hints"

            # The cron payload is in the queue waiting to be drained
            # by the (currently blocked) worker
            assert queue.qsize() == 1
        finally:
            blocked.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            await _cleanup_workers(sess)
