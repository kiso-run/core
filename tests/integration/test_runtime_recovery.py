"""Startup recovery end-to-end.

These tests exercise ``kiso.main._startup_recovery`` against a real
ASGI app + worker, not just the store helpers (which
``tests/test_startup_recovery.py`` already covers at the helper level).

The goal is to prove that, after a simulated server restart with
trusted unprocessed messages and stale running plans in the DB, the
runtime actually:

- marks stale plans/tasks as failed,
- re-enqueues the unprocessed messages,
- spawns workers and drains them,
- delivers the resulting final webhooks,
- does not double-process on a second startup recovery call.
"""

from __future__ import annotations

import httpx
import pytest

from kiso.main import _startup_recovery, _workers, _worker_phases, app
from kiso.store import (
    create_session,
    create_plan,
    save_message,
)

from tests.integration.conftest import wait_for_worker_idle


pytestmark = pytest.mark.integration


async def _drain_all(client: httpx.AsyncClient, sessions: list[str], timeout: float = 15.0):
    for sess in sessions:
        try:
            await wait_for_worker_idle(client, sess, timeout=timeout)
        except TimeoutError:
            # Worker may have already finished and cleaned up
            pass


async def _cleanup_workers(*sessions: str):
    """Pop any leftover worker entries between tests."""
    for sess in sessions:
        entry = _workers.pop(sess, None)
        _worker_phases.pop(sess, None)
        if entry is not None and not entry.task.done():
            entry.task.cancel()


class TestStartupRecoveryEndToEnd:

    async def test_re_enqueues_single_unprocessed_message(
        self, kiso_client: httpx.AsyncClient, integration_db, webhook_collector,
    ):
        """A trusted unprocessed message is re-enqueued and processed end-to-end.

        The mocked planner returns a msg-only plan which fails the codegen
        guardrail; the worker handles the error and still marks the message
        as processed and delivers a final webhook. We only care that the
        recovery pipeline drove the message through to a terminal state.
        """
        sess = "recover-single"
        await create_session(integration_db, sess, webhook="http://test/recover")
        msg_id = await save_message(
            integration_db, sess, "testuser", "user", "Recover me",
            trusted=True, processed=False,
        )

        config = app.state.config
        await _startup_recovery(integration_db, config)

        await _drain_all(kiso_client, [sess])
        await _cleanup_workers(sess)

        # At least one final webhook delivered for the session
        finals = [d for d in webhook_collector.deliveries
                  if d["session"] == sess and d["final"]]
        assert len(finals) >= 1

        # The seeded message itself was marked processed
        cur = await integration_db.execute(
            "SELECT processed FROM messages WHERE id = ?", (msg_id,)
        )
        row = await cur.fetchone()
        assert row["processed"] == 1

    async def test_re_enqueues_across_multiple_sessions(
        self, kiso_client: httpx.AsyncClient, integration_db, webhook_collector,
    ):
        """Recovery handles multiple sessions independently and processes each."""
        sessions = ["recover-multi-a", "recover-multi-b"]
        msg_ids: dict[str, int] = {}
        for sess in sessions:
            await create_session(integration_db, sess, webhook=f"http://test/{sess}")
            msg_ids[sess] = await save_message(
                integration_db, sess, "testuser", "user", f"hello from {sess}",
                trusted=True, processed=False,
            )

        config = app.state.config
        await _startup_recovery(integration_db, config)

        await _drain_all(kiso_client, sessions)
        await _cleanup_workers(*sessions)

        delivered_sessions = {d["session"] for d in webhook_collector.deliveries}
        assert delivered_sessions == set(sessions)

        for sess, msg_id in msg_ids.items():
            cur = await integration_db.execute(
                "SELECT processed FROM messages WHERE id = ?", (msg_id,)
            )
            row = await cur.fetchone()
            assert row["processed"] == 1, f"message {msg_id} for {sess} not processed"

    async def test_marks_stale_running_plans_and_tasks_failed(
        self, kiso_client: httpx.AsyncClient, integration_db,
    ):
        """Stale running plans/tasks are marked failed by recovery."""
        sess = "recover-stale"
        await create_session(integration_db, sess)
        msg_id = await save_message(
            integration_db, sess, "testuser", "user", "stale plan owner",
            trusted=False,  # untrusted so we don't also re-enqueue it
            processed=True,
        )
        plan_id = await create_plan(integration_db, sess, msg_id, "stale goal")
        await integration_db.execute(
            "UPDATE plans SET status = 'running' WHERE id = ?", (plan_id,)
        )
        await integration_db.execute(
            "INSERT INTO tasks (plan_id, session, type, detail, status) "
            "VALUES (?, ?, 'msg', 'stale task', 'running')",
            (plan_id, sess),
        )
        await integration_db.commit()

        config = app.state.config
        await _startup_recovery(integration_db, config)

        cur = await integration_db.execute(
            "SELECT status FROM plans WHERE id = ?", (plan_id,)
        )
        plan_row = await cur.fetchone()
        assert plan_row["status"] == "failed"

        cur = await integration_db.execute(
            "SELECT status, output FROM tasks WHERE plan_id = ?", (plan_id,)
        )
        task_rows = await cur.fetchall()
        assert all(row["status"] == "failed" for row in task_rows)
        assert any("Server restarted" in (row["output"] or "") for row in task_rows)

    async def test_repeated_recovery_does_not_double_process(
        self, kiso_client: httpx.AsyncClient, integration_db, webhook_collector,
    ):
        """Calling recovery a second time after drain must not re-enqueue."""
        sess = "recover-idempotent"
        await create_session(integration_db, sess, webhook="http://test/idem")
        msg_id = await save_message(
            integration_db, sess, "testuser", "user", "process once",
            trusted=True, processed=False,
        )

        config = app.state.config
        await _startup_recovery(integration_db, config)
        await _drain_all(kiso_client, [sess])
        await _cleanup_workers(sess)

        deliveries_after_first = len(webhook_collector.deliveries)
        assert deliveries_after_first >= 1

        # Sanity: the seeded message is processed
        cur = await integration_db.execute(
            "SELECT processed FROM messages WHERE id = ?", (msg_id,)
        )
        assert (await cur.fetchone())["processed"] == 1

        # Second recovery: message is already processed=1, so nothing to enqueue
        await _startup_recovery(integration_db, config)
        await _drain_all(kiso_client, [sess], timeout=2.0)
        await _cleanup_workers(sess)

        # No new deliveries from the second pass
        assert len(webhook_collector.deliveries) == deliveries_after_first
