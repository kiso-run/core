"""Tests for startup recovery — stale plans/tasks and unprocessed messages."""

from __future__ import annotations

import pytest

from kiso.store import (
    create_plan,
    create_session,
    create_task,
    get_unprocessed_trusted_messages,
    init_db,
    recover_stale_running,
    save_message,
    update_plan_status,
    update_task,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


class TestGetUnprocessedTrustedMessages:
    async def test_returns_trusted_unprocessed(self, db):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "hello", trusted=True, processed=False)
        msgs = await get_unprocessed_trusted_messages(db)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"
        assert msgs[0]["trusted"] == 1
        assert msgs[0]["processed"] == 0

    async def test_excludes_untrusted(self, db):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "bob", "user", "untrusted msg", trusted=False, processed=False)
        msgs = await get_unprocessed_trusted_messages(db)
        assert len(msgs) == 0

    async def test_excludes_processed(self, db):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "already done", trusted=True, processed=True)
        msgs = await get_unprocessed_trusted_messages(db)
        assert len(msgs) == 0

    async def test_ordered_by_id(self, db):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "first", trusted=True, processed=False)
        await save_message(db, "sess1", "alice", "user", "second", trusted=True, processed=False)
        await save_message(db, "sess1", "alice", "user", "third", trusted=True, processed=False)
        msgs = await get_unprocessed_trusted_messages(db)
        assert [m["content"] for m in msgs] == ["first", "second", "third"]
        assert msgs[0]["id"] < msgs[1]["id"] < msgs[2]["id"]


class TestRecoverStaleRunning:
    async def test_stale_running_plans_marked_failed(self, db):
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "test", processed=True)
        plan_id = await create_plan(db, "sess1", msg_id, "test goal")
        # plan is created with status='running' by default

        plans_count, tasks_count = await recover_stale_running(db)
        assert plans_count == 1
        assert tasks_count == 0

        cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        assert row[0] == "failed"

    async def test_stale_running_tasks_marked_failed(self, db):
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "test", processed=True)
        plan_id = await create_plan(db, "sess1", msg_id, "test goal")
        task_id = await create_task(db, plan_id, "sess1", "exec", "echo hi")
        await update_task(db, task_id, "running")

        plans_count, tasks_count = await recover_stale_running(db)
        assert plans_count == 1  # plan was also running
        assert tasks_count == 1

        cur = await db.execute("SELECT status, output FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        assert row[0] == "failed"
        assert row[1] == "Server restarted"

    async def test_done_plans_untouched(self, db):
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "test", processed=True)
        plan_id = await create_plan(db, "sess1", msg_id, "test goal")
        await update_plan_status(db, plan_id, "done")

        plans_count, tasks_count = await recover_stale_running(db)
        assert plans_count == 0
        assert tasks_count == 0

        cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        assert row[0] == "done"
