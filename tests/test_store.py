"""Tests for kiso/store.py."""

from __future__ import annotations

import aiosqlite

from kiso.store import (
    create_plan,
    create_session,
    create_task,
    get_all_sessions,
    get_facts,
    get_pending_items,
    get_plan_for_session,
    get_recent_messages,
    get_session,
    get_sessions_for_user,
    get_tasks_for_plan,
    get_tasks_for_session,
    get_unprocessed_messages,
    mark_message_processed,
    save_message,
    update_plan_status,
    update_task,
)


async def test_init_creates_tables(db: aiosqlite.Connection):
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = sorted(
        r[0] for r in await cur.fetchall() if not r[0].startswith("sqlite_")
    )
    expected = [
        "facts", "learnings", "messages", "pending",
        "plans", "published", "sessions", "tasks",
    ]
    assert tables == expected


async def test_create_and_get_session(db: aiosqlite.Connection):
    result = await create_session(db, "sess1", connector="cli")
    assert result["session"] == "sess1"
    assert result["connector"] == "cli"

    fetched = await get_session(db, "sess1")
    assert fetched is not None
    assert fetched["session"] == "sess1"


async def test_create_session_idempotent(db: aiosqlite.Connection):
    s1 = await create_session(db, "sess1", connector="cli")
    s2 = await create_session(db, "sess1", connector="other")
    assert s1["session"] == s2["session"]
    # connector should remain "cli" (first create wins)
    assert s2["connector"] == "cli"


async def test_get_session_missing(db: aiosqlite.Connection):
    assert await get_session(db, "nonexistent") is None


async def test_save_message_returns_id(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    msg_id = await save_message(db, "sess1", "alice", "user", "hello")
    assert isinstance(msg_id, int)
    assert msg_id > 0


async def test_mark_processed(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
    unprocessed = await get_unprocessed_messages(db)
    assert len(unprocessed) == 1

    await mark_message_processed(db, msg_id)
    unprocessed = await get_unprocessed_messages(db)
    assert len(unprocessed) == 0


async def test_unprocessed_excludes_untrusted(db: aiosqlite.Connection):
    """Untrusted messages saved with processed=True are never unprocessed."""
    await create_session(db, "sess1")
    await save_message(db, "sess1", "stranger", "user", "hi", trusted=False, processed=True)
    await save_message(db, "sess1", "alice", "user", "hello", trusted=True, processed=False)
    unprocessed = await get_unprocessed_messages(db)
    assert len(unprocessed) == 1
    assert unprocessed[0]["user"] == "alice"


async def test_sessions_for_user(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await create_session(db, "sess2")
    await save_message(db, "sess1", "alice", "user", "hi")
    await save_message(db, "sess2", "bob", "user", "hi")

    alice_sessions = await get_sessions_for_user(db, "alice")
    assert len(alice_sessions) == 1
    assert alice_sessions[0]["session"] == "sess1"


async def test_all_sessions(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await create_session(db, "sess2")
    all_sess = await get_all_sessions(db)
    assert len(all_sess) == 2


async def test_sessions_for_user_no_messages(db: aiosqlite.Connection):
    """User with no messages gets empty list."""
    await create_session(db, "sess1")
    sessions = await get_sessions_for_user(db, "ghost")
    assert sessions == []


async def test_tasks_empty(db: aiosqlite.Connection):
    tasks = await get_tasks_for_session(db, "nonexistent")
    assert tasks == []


async def test_tasks_after_filter(db: aiosqlite.Connection):
    """after parameter filters tasks by id."""
    await create_session(db, "sess1")
    # Insert tasks directly to test the filter
    for i in range(3):
        await db.execute(
            "INSERT INTO tasks (plan_id, session, type, detail) VALUES (?, ?, ?, ?)",
            (1, "sess1", "exec", f"task {i}"),
        )
    await db.commit()

    all_tasks = await get_tasks_for_session(db, "sess1")
    assert len(all_tasks) == 3

    after_first = await get_tasks_for_session(db, "sess1", after=all_tasks[0]["id"])
    assert len(after_first) == 2

    after_last = await get_tasks_for_session(db, "sess1", after=all_tasks[-1]["id"])
    assert after_last == []


async def test_plan_none(db: aiosqlite.Connection):
    plan = await get_plan_for_session(db, "nonexistent")
    assert plan is None


async def test_wal_mode_enabled(db: aiosqlite.Connection):
    cur = await db.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    assert row[0] == "wal"


# --- get_recent_messages ---

async def test_recent_messages_trusted_only(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await save_message(db, "sess1", "alice", "user", "trusted", trusted=True)
    await save_message(db, "sess1", "stranger", "user", "untrusted", trusted=False, processed=True)
    recent = await get_recent_messages(db, "sess1", limit=10)
    assert len(recent) == 1
    assert recent[0]["content"] == "trusted"


async def test_recent_messages_oldest_first(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await save_message(db, "sess1", "alice", "user", "first")
    await save_message(db, "sess1", "alice", "user", "second")
    await save_message(db, "sess1", "alice", "user", "third")
    recent = await get_recent_messages(db, "sess1", limit=10)
    assert [r["content"] for r in recent] == ["first", "second", "third"]


async def test_recent_messages_respects_limit(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    for i in range(5):
        await save_message(db, "sess1", "alice", "user", f"msg-{i}")
    recent = await get_recent_messages(db, "sess1", limit=2)
    assert len(recent) == 2
    assert recent[0]["content"] == "msg-3"
    assert recent[1]["content"] == "msg-4"


async def test_recent_messages_empty_session(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    recent = await get_recent_messages(db, "sess1")
    assert recent == []


# --- get_facts ---

async def test_get_facts_empty(db: aiosqlite.Connection):
    facts = await get_facts(db)
    assert facts == []


async def test_get_facts_returns_all(db: aiosqlite.Connection):
    await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Fact 1", "curator"))
    await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Fact 2", "manual"))
    await db.commit()
    facts = await get_facts(db)
    assert len(facts) == 2
    assert facts[0]["content"] == "Fact 1"
    assert facts[1]["content"] == "Fact 2"


# --- get_pending_items ---

async def test_pending_items_global_and_session(db: aiosqlite.Connection):
    await db.execute("INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)", ("Global Q", "global", "curator"))
    await db.execute("INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)", ("Session Q", "sess1", "planner"))
    await db.execute("INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)", ("Other Q", "sess2", "planner"))
    await db.commit()
    items = await get_pending_items(db, "sess1")
    assert len(items) == 2
    contents = {i["content"] for i in items}
    assert "Global Q" in contents
    assert "Session Q" in contents
    assert "Other Q" not in contents


async def test_pending_items_excludes_resolved(db: aiosqlite.Connection):
    await db.execute(
        "INSERT INTO pending (content, scope, source, status) VALUES (?, ?, ?, ?)",
        ("Resolved", "global", "curator", "resolved"),
    )
    await db.execute(
        "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
        ("Open", "global", "curator"),
    )
    await db.commit()
    items = await get_pending_items(db, "sess1")
    assert len(items) == 1
    assert items[0]["content"] == "Open"


# --- create_plan / update_plan_status ---

async def test_create_plan_returns_id(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test goal")
    assert isinstance(plan_id, int)
    assert plan_id > 0


async def test_create_plan_default_running(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    plan = await get_plan_for_session(db, "sess1")
    assert plan["status"] == "running"
    assert plan["goal"] == "Test"


async def test_create_plan_with_parent(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    p1 = await create_plan(db, "sess1", message_id=1, goal="First")
    p2 = await create_plan(db, "sess1", message_id=1, goal="Replan", parent_id=p1)
    plan = await get_plan_for_session(db, "sess1")
    assert plan["id"] == p2
    assert plan["parent_id"] == p1


async def test_update_plan_status(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await update_plan_status(db, plan_id, "done")
    plan = await get_plan_for_session(db, "sess1")
    assert plan["status"] == "done"


# --- create_task / update_task / get_tasks_for_plan ---

async def test_create_task_returns_id(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="ls", expect="files")
    assert isinstance(task_id, int)
    assert task_id > 0


async def test_create_task_all_fields(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(
        db, plan_id, "sess1", type="skill", detail="search web",
        skill="search", args='{"query": "test"}', expect="results found",
    )
    tasks = await get_tasks_for_plan(db, plan_id)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["type"] == "skill"
    assert t["skill"] == "search"
    assert t["args"] == '{"query": "test"}'
    assert t["expect"] == "results found"
    assert t["status"] == "pending"


async def test_update_task_status_and_output(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="ls")
    await update_task(db, task_id, "done", output="file1\nfile2", stderr="")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["status"] == "done"
    assert tasks[0]["output"] == "file1\nfile2"
    assert tasks[0]["stderr"] == ""


async def test_update_task_failed_with_stderr(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="bad")
    await update_task(db, task_id, "failed", output="", stderr="command not found")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["status"] == "failed"
    assert tasks[0]["stderr"] == "command not found"


async def test_get_tasks_for_plan_ordered(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    id1 = await create_task(db, plan_id, "sess1", type="exec", detail="first")
    id2 = await create_task(db, plan_id, "sess1", type="msg", detail="second")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert len(tasks) == 2
    assert tasks[0]["id"] == id1
    assert tasks[1]["id"] == id2


async def test_get_tasks_for_plan_empty(db: aiosqlite.Connection):
    tasks = await get_tasks_for_plan(db, 999)
    assert tasks == []
