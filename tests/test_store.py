"""Tests for kiso/store.py."""

from __future__ import annotations

import aiosqlite

from kiso.store import (
    create_session,
    get_all_sessions,
    get_plan_for_session,
    get_session,
    get_sessions_for_user,
    get_tasks_for_session,
    get_unprocessed_messages,
    mark_message_processed,
    save_message,
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
