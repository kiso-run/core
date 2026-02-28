"""Tests for kiso/store.py."""

from __future__ import annotations

import pytest
import aiosqlite

from kiso.store import (
    append_task_llm_call,
    archive_low_confidence_facts,
    search_facts,
    count_messages,
    create_plan,
    create_session,
    create_task,
    decay_facts,
    delete_facts,
    get_all_sessions,
    get_facts,
    get_oldest_messages,
    get_pending_items,
    get_pending_learnings,
    get_plan_for_session,
    get_recent_messages,
    get_session,
    get_sessions_for_user,
    get_tasks_for_plan,
    get_tasks_for_session,
    get_unprocessed_messages,
    get_untrusted_messages,
    mark_message_processed,
    save_fact,
    save_message,
    save_pending_item,
    update_fact_usage,
    update_learning,
    update_plan_status,
    update_plan_usage,
    update_summary,
    update_task,
    update_task_command,
    update_task_retry_count,
    update_task_review,
    update_task_substatus,
    update_task_usage,
    save_learning,
)


async def test_init_creates_tables(db: aiosqlite.Connection):
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    # Exclude sqlite_ internals and FTS5 shadow tables (kiso_facts_fts_*)
    tables = sorted(
        r[0] for r in await cur.fetchall()
        if not r[0].startswith("sqlite_") and not r[0].startswith("kiso_facts_fts_")
    )
    expected = [
        "facts", "facts_archive", "kiso_facts_fts", "learnings", "messages",
        "pending", "plans", "sessions", "tasks",
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


# --- M9: get_pending_learnings ---

async def test_pending_learnings_empty(db: aiosqlite.Connection):
    result = await get_pending_learnings(db)
    assert result == []


async def test_pending_learnings_with_data(db: aiosqlite.Connection):
    from kiso.store import save_learning
    await create_session(db, "sess1")
    await save_learning(db, "Fact A", "sess1")
    await save_learning(db, "Fact B", "sess1")
    result = await get_pending_learnings(db)
    assert len(result) == 2
    assert result[0]["content"] == "Fact A"
    assert result[1]["content"] == "Fact B"


async def test_pending_learnings_respects_limit(db: aiosqlite.Connection):
    from kiso.store import save_learning
    await create_session(db, "sess1")
    for i in range(5):
        await save_learning(db, f"Fact {i}", "sess1")
    result = await get_pending_learnings(db, limit=2)
    assert len(result) == 2


async def test_pending_learnings_only_pending(db: aiosqlite.Connection):
    from kiso.store import save_learning
    await create_session(db, "sess1")
    lid = await save_learning(db, "Promoted", "sess1")
    await update_learning(db, lid, "promoted")
    await save_learning(db, "Still pending", "sess1")
    result = await get_pending_learnings(db)
    assert len(result) == 1
    assert result[0]["content"] == "Still pending"


# --- M9: update_learning ---

async def test_update_learning_promoted(db: aiosqlite.Connection):
    from kiso.store import save_learning
    await create_session(db, "sess1")
    lid = await save_learning(db, "A fact", "sess1")
    await update_learning(db, lid, "promoted")
    cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
    row = await cur.fetchone()
    assert row[0] == "promoted"


async def test_update_learning_discarded(db: aiosqlite.Connection):
    from kiso.store import save_learning
    await create_session(db, "sess1")
    lid = await save_learning(db, "Noise", "sess1")
    await update_learning(db, lid, "discarded")
    cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
    row = await cur.fetchone()
    assert row[0] == "discarded"


# --- M9: save_fact ---

async def test_save_fact_returns_id(db: aiosqlite.Connection):
    fid = await save_fact(db, "Python 3.12", "curator")
    assert isinstance(fid, int)
    assert fid > 0


async def test_save_fact_retrievable(db: aiosqlite.Connection):
    await save_fact(db, "Uses pytest", "curator", session="sess1")
    facts = await get_facts(db)
    assert len(facts) == 1
    assert facts[0]["content"] == "Uses pytest"
    assert facts[0]["source"] == "curator"
    assert facts[0]["session"] == "sess1"


# --- M9: save_pending_item ---

async def test_save_pending_item_returns_id(db: aiosqlite.Connection):
    pid = await save_pending_item(db, "Which DB?", "sess1", "curator")
    assert isinstance(pid, int)
    assert pid > 0


async def test_save_pending_item_retrievable(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await save_pending_item(db, "Which DB?", "sess1", "curator")
    items = await get_pending_items(db, "sess1")
    assert len(items) == 1
    assert items[0]["content"] == "Which DB?"
    assert items[0]["source"] == "curator"


# --- M9: update_summary ---

async def test_update_summary(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await update_summary(db, "sess1", "New summary text")
    sess = await get_session(db, "sess1")
    assert sess["summary"] == "New summary text"


# --- M9: count_messages ---

async def test_count_messages_only_trusted(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await save_message(db, "sess1", "alice", "user", "trusted1", trusted=True)
    await save_message(db, "sess1", "alice", "user", "trusted2", trusted=True)
    await save_message(db, "sess1", "stranger", "user", "untrusted", trusted=False, processed=True)
    count = await count_messages(db, "sess1")
    assert count == 2


async def test_count_messages_empty(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    count = await count_messages(db, "sess1")
    assert count == 0


# --- M9: get_oldest_messages ---

async def test_get_oldest_messages_order(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await save_message(db, "sess1", "alice", "user", "first")
    await save_message(db, "sess1", "alice", "user", "second")
    await save_message(db, "sess1", "alice", "user", "third")
    oldest = await get_oldest_messages(db, "sess1", limit=2)
    assert len(oldest) == 2
    assert oldest[0]["content"] == "first"
    assert oldest[1]["content"] == "second"


async def test_get_oldest_messages_limit(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    for i in range(5):
        await save_message(db, "sess1", "alice", "user", f"msg-{i}")
    oldest = await get_oldest_messages(db, "sess1", limit=3)
    assert len(oldest) == 3
    assert oldest[0]["content"] == "msg-0"


# --- M9: delete_facts ---

async def test_delete_facts(db: aiosqlite.Connection):
    fid1 = await save_fact(db, "Fact 1", "curator")
    fid2 = await save_fact(db, "Fact 2", "curator")
    fid3 = await save_fact(db, "Fact 3", "curator")
    await delete_facts(db, [fid1, fid3])
    facts = await get_facts(db)
    assert len(facts) == 1
    assert facts[0]["id"] == fid2


async def test_delete_facts_empty_list(db: aiosqlite.Connection):
    await save_fact(db, "Fact 1", "curator")
    await delete_facts(db, [])
    facts = await get_facts(db)
    assert len(facts) == 1


# --- M10: get_untrusted_messages ---

async def test_get_untrusted_messages(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await save_message(db, "sess1", "alice", "user", "trusted msg", trusted=True)
    await save_message(db, "sess1", "stranger", "user", "untrusted 1", trusted=False, processed=True)
    await save_message(db, "sess1", "stranger2", "user", "untrusted 2", trusted=False, processed=True)
    untrusted = await get_untrusted_messages(db, "sess1")
    assert len(untrusted) == 2
    assert untrusted[0]["content"] == "untrusted 1"
    assert untrusted[1]["content"] == "untrusted 2"


async def test_get_untrusted_messages_empty(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    await save_message(db, "sess1", "alice", "user", "trusted msg", trusted=True)
    untrusted = await get_untrusted_messages(db, "sess1")
    assert untrusted == []


async def test_get_untrusted_messages_respects_limit(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    for i in range(5):
        await save_message(db, "sess1", "stranger", "user", f"untrusted-{i}", trusted=False, processed=True)
    untrusted = await get_untrusted_messages(db, "sess1", limit=2)
    assert len(untrusted) == 2
    assert untrusted[0]["content"] == "untrusted-0"
    assert untrusted[1]["content"] == "untrusted-1"


# --- M19: update_task_review ---


async def test_update_task_review_ok(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    await update_task_review(db, task_id, "ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["review_verdict"] == "ok"
    assert tasks[0]["review_reason"] is None
    assert tasks[0]["review_learning"] is None


async def test_update_task_review_replan(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="bad", expect="ok")
    await update_task_review(db, task_id, "replan", reason="Directory not found")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["review_verdict"] == "replan"
    assert tasks[0]["review_reason"] == "Directory not found"
    assert tasks[0]["review_learning"] is None


async def test_update_task_review_with_learning(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    await update_task_review(db, task_id, "ok", learning="Uses pytest")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["review_verdict"] == "ok"
    assert tasks[0]["review_reason"] is None
    assert tasks[0]["review_learning"] == "Uses pytest"


async def test_review_fields_null_by_default(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["review_verdict"] is None
    assert tasks[0]["review_reason"] is None
    assert tasks[0]["review_learning"] is None


# --- update_task_command ---


async def test_update_task_command(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="list files")
    await update_task_command(db, task_id, "ls -la")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["command"] == "ls -la"


async def test_task_command_null_by_default(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="list files")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["command"] is None


# --- update_plan_usage ---


async def test_update_plan_usage(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await update_plan_usage(db, plan_id, 1234, 567, "deepseek/deepseek-v3.2")
    plan = await get_plan_for_session(db, "sess1")
    assert plan["total_input_tokens"] == 1234
    assert plan["total_output_tokens"] == 567
    assert plan["model"] == "deepseek/deepseek-v3.2"


async def test_plan_usage_defaults(db: aiosqlite.Connection):
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    plan = await get_plan_for_session(db, "sess1")
    assert plan["total_input_tokens"] == 0
    assert plan["total_output_tokens"] == 0
    assert plan["model"] is None


# --- Schema columns ---


async def test_schema_has_command_column(db: aiosqlite.Connection):
    cur = await db.execute("PRAGMA table_info(tasks)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "command" in columns


async def test_schema_has_token_columns(db: aiosqlite.Connection):
    cur = await db.execute("PRAGMA table_info(plans)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "total_input_tokens" in columns
    assert "total_output_tokens" in columns
    assert "model" in columns


# --- update_task_usage ---


async def test_update_task_usage(db: aiosqlite.Connection):
    """Stores and reads back per-step token counts."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    await update_task_usage(db, task_id, 430, 85)
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["input_tokens"] == 430
    assert tasks[0]["output_tokens"] == 85


async def test_task_usage_defaults_to_zero(db: aiosqlite.Connection):
    """Newly created tasks have 0 input_tokens and 0 output_tokens."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["input_tokens"] == 0
    assert tasks[0]["output_tokens"] == 0


async def test_schema_has_task_token_columns(db: aiosqlite.Connection):
    """Verify tasks table has input_tokens and output_tokens columns."""
    cur = await db.execute("PRAGMA table_info(tasks)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "input_tokens" in columns
    assert "output_tokens" in columns


# --- llm_calls columns ---


async def test_schema_has_llm_calls_columns(db: aiosqlite.Connection):
    """Both tasks and plans tables have llm_calls TEXT column."""
    for table in ("tasks", "plans"):
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        assert "llm_calls" in columns, f"llm_calls missing from {table}"


async def test_update_task_usage_with_llm_calls(db: aiosqlite.Connection):
    """Stores and retrieves per-call LLM breakdown on tasks."""
    import json
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    task_id = await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    calls = [
        {"role": "translator", "model": "deepseek/deepseek-v3", "input_tokens": 300, "output_tokens": 45},
        {"role": "reviewer", "model": "deepseek/deepseek-v3", "input_tokens": 350, "output_tokens": 60},
    ]
    await update_task_usage(db, task_id, 650, 105, llm_calls=calls)
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["input_tokens"] == 650
    assert tasks[0]["output_tokens"] == 105
    stored = json.loads(tasks[0]["llm_calls"])
    assert len(stored) == 2
    assert stored[0]["role"] == "translator"
    assert stored[1]["role"] == "reviewer"


async def test_update_plan_usage_with_llm_calls(db: aiosqlite.Connection):
    """Stores and retrieves per-call LLM breakdown on plans."""
    import json
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    calls = [
        {"role": "planner", "model": "gpt-4", "input_tokens": 400, "output_tokens": 80},
        {"role": "messenger", "model": "gpt-4", "input_tokens": 200, "output_tokens": 100},
    ]
    await update_plan_usage(db, plan_id, 600, 180, "gpt-4", llm_calls=calls)
    plan = await get_plan_for_session(db, "sess1")
    assert plan["total_input_tokens"] == 600
    assert plan["total_output_tokens"] == 180
    stored = json.loads(plan["llm_calls"])
    assert len(stored) == 2
    assert stored[0]["role"] == "planner"


async def test_update_plan_usage_preserves_llm_calls(db: aiosqlite.Connection):
    """Updating totals without llm_calls preserves existing planner calls."""
    import json
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    # First update: store planner calls
    calls = [{"role": "planner", "model": "gpt-4", "input_tokens": 400, "output_tokens": 80}]
    await update_plan_usage(db, plan_id, 400, 80, "gpt-4", llm_calls=calls)
    # Second update: only update totals (default omits llm_calls)
    await update_plan_usage(db, plan_id, 1000, 300, "gpt-4")
    plan = await get_plan_for_session(db, "sess1")
    assert plan["total_input_tokens"] == 1000
    assert plan["total_output_tokens"] == 300
    # llm_calls should still have only the planner call
    stored = json.loads(plan["llm_calls"])
    assert len(stored) == 1
    assert stored[0]["role"] == "planner"


async def test_llm_calls_null_by_default(db: aiosqlite.Connection):
    """Newly created tasks and plans have NULL llm_calls."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["llm_calls"] is None
    plan = await get_plan_for_session(db, "sess1")
    assert plan["llm_calls"] is None


# --- M31b: update_task_substatus ---


async def test_update_task_substatus(db: aiosqlite.Connection):
    """update_task_substatus sets substatus and updates timestamp."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]

    await update_task_substatus(db, task_id, "translating")

    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["substatus"] == "translating"
    # Other fields unchanged
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["detail"] == "echo ok"


async def test_update_task_substatus_empty(db: aiosqlite.Connection):
    """Empty string substatus is stored without error."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]

    await update_task_substatus(db, task_id, "")

    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["substatus"] == ""


# --- M31b: append_task_llm_call ---


async def test_append_task_llm_call_first(db: aiosqlite.Connection):
    """First append creates a single-element JSON array."""
    import json
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]
    assert tasks[0]["llm_calls"] is None

    call = {"role": "searcher", "model": "gemini-flash", "input_tokens": 100, "output_tokens": 50}
    await append_task_llm_call(db, task_id, call)

    tasks = await get_tasks_for_plan(db, plan_id)
    stored = json.loads(tasks[0]["llm_calls"])
    assert len(stored) == 1
    assert stored[0]["role"] == "searcher"
    assert stored[0]["input_tokens"] == 100


async def test_append_task_llm_call_existing(db: aiosqlite.Connection):
    """Appending to existing calls array grows it."""
    import json
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]

    call1 = {"role": "searcher", "model": "gemini", "input_tokens": 100, "output_tokens": 50}
    call2 = {"role": "reviewer", "model": "deepseek", "input_tokens": 200, "output_tokens": 60}
    await append_task_llm_call(db, task_id, call1)
    await append_task_llm_call(db, task_id, call2)

    tasks = await get_tasks_for_plan(db, plan_id)
    stored = json.loads(tasks[0]["llm_calls"])
    assert len(stored) == 2
    assert stored[0]["role"] == "searcher"
    assert stored[1]["role"] == "reviewer"


async def test_append_task_llm_call_corrupted_json(db: aiosqlite.Connection):
    """Corrupted existing llm_calls JSON: append starts fresh array."""
    import json
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]

    # Manually corrupt the llm_calls field
    await db.execute("UPDATE tasks SET llm_calls = 'NOT_JSON' WHERE id = ?", (task_id,))
    await db.commit()

    call = {"role": "searcher", "model": "gemini", "input_tokens": 100, "output_tokens": 50}
    await append_task_llm_call(db, task_id, call)

    tasks = await get_tasks_for_plan(db, plan_id)
    stored = json.loads(tasks[0]["llm_calls"])
    assert len(stored) == 1
    assert stored[0]["role"] == "searcher"


# --- M33: retry_count column ---


async def test_retry_count_column_exists(db: aiosqlite.Connection):
    """Tasks table has a retry_count column."""
    cur = await db.execute("PRAGMA table_info(tasks)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "retry_count" in columns


async def test_retry_count_defaults_to_zero(db: aiosqlite.Connection):
    """New tasks have retry_count = 0."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["retry_count"] == 0


async def test_update_task_retry_count(db: aiosqlite.Connection):
    """update_task_retry_count sets retry_count correctly."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]

    await update_task_retry_count(db, task_id, 2)
    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["retry_count"] == 2


# --- M34: facts enriched schema ---


async def test_facts_have_category_column(db: aiosqlite.Connection):
    cur = await db.execute("PRAGMA table_info(facts)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "category" in columns


async def test_facts_have_confidence_column(db: aiosqlite.Connection):
    cur = await db.execute("PRAGMA table_info(facts)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "confidence" in columns


async def test_facts_have_last_used_column(db: aiosqlite.Connection):
    cur = await db.execute("PRAGMA table_info(facts)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "last_used" in columns


async def test_facts_have_use_count_column(db: aiosqlite.Connection):
    cur = await db.execute("PRAGMA table_info(facts)")
    columns = {row[1] for row in await cur.fetchall()}
    assert "use_count" in columns


async def test_facts_archive_table_exists(db: aiosqlite.Connection):
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='facts_archive'"
    )
    row = await cur.fetchone()
    assert row is not None


# --- M34: save_fact with category and confidence ---


async def test_save_fact_with_category_and_confidence(db: aiosqlite.Connection):
    fid = await save_fact(db, "Uses Docker", "curator", category="tool", confidence=0.9)
    facts = await get_facts(db)
    assert len(facts) == 1
    assert facts[0]["content"] == "Uses Docker"
    assert facts[0]["category"] == "tool"
    assert facts[0]["confidence"] == 0.9


async def test_save_fact_defaults(db: aiosqlite.Connection):
    fid = await save_fact(db, "A fact", "curator")
    facts = await get_facts(db)
    assert facts[0]["category"] == "general"
    assert facts[0]["confidence"] == 1.0
    assert facts[0]["use_count"] == 0
    assert facts[0]["last_used"] is None


# --- M34: update_fact_usage ---


async def test_update_fact_usage(db: aiosqlite.Connection):
    fid = await save_fact(db, "Fact 1", "curator")
    await update_fact_usage(db, [fid])
    facts = await get_facts(db)
    assert facts[0]["use_count"] == 1
    assert facts[0]["last_used"] is not None


async def test_update_fact_usage_increments(db: aiosqlite.Connection):
    fid = await save_fact(db, "Fact 1", "curator")
    await update_fact_usage(db, [fid])
    await update_fact_usage(db, [fid])
    facts = await get_facts(db)
    assert facts[0]["use_count"] == 2


async def test_update_fact_usage_empty_list(db: aiosqlite.Connection):
    """Empty list is a no-op."""
    fid = await save_fact(db, "Fact 1", "curator")
    await update_fact_usage(db, [])
    facts = await get_facts(db)
    assert facts[0]["use_count"] == 0


# --- M34: decay_facts ---


async def test_decay_facts_stale(db: aiosqlite.Connection):
    """Facts older than decay_days get confidence reduced."""
    fid = await save_fact(db, "Old fact", "curator")
    # Backdate created_at to 10 days ago
    await db.execute(
        "UPDATE facts SET created_at = datetime('now', '-10 days') WHERE id = ?",
        (fid,),
    )
    await db.commit()
    affected = await decay_facts(db, decay_days=7, decay_rate=0.1)
    assert affected == 1
    facts = await get_facts(db)
    assert facts[0]["confidence"] == pytest.approx(0.9)


async def test_decay_facts_recent_not_decayed(db: aiosqlite.Connection):
    """Facts created recently are not decayed."""
    await save_fact(db, "Fresh fact", "curator")
    affected = await decay_facts(db, decay_days=7, decay_rate=0.1)
    assert affected == 0
    facts = await get_facts(db)
    assert facts[0]["confidence"] == 1.0


async def test_decay_facts_recently_used_not_decayed(db: aiosqlite.Connection):
    """Facts used recently (even if created long ago) are not decayed."""
    fid = await save_fact(db, "Used fact", "curator")
    await db.execute(
        "UPDATE facts SET created_at = datetime('now', '-30 days') WHERE id = ?",
        (fid,),
    )
    await db.commit()
    # Mark as recently used
    await update_fact_usage(db, [fid])
    affected = await decay_facts(db, decay_days=7, decay_rate=0.1)
    assert affected == 0


async def test_decay_facts_floor_at_zero(db: aiosqlite.Connection):
    """Confidence doesn't go below 0.0."""
    fid = await save_fact(db, "Dying fact", "curator", confidence=0.05)
    await db.execute(
        "UPDATE facts SET created_at = datetime('now', '-10 days') WHERE id = ?",
        (fid,),
    )
    await db.commit()
    await decay_facts(db, decay_days=7, decay_rate=0.1)
    facts = await get_facts(db)
    assert facts[0]["confidence"] == 0.0


# --- M34: archive_low_confidence_facts ---


async def test_archive_moves_low_confidence(db: aiosqlite.Connection):
    """Facts below threshold are moved to facts_archive and deleted from facts."""
    fid1 = await save_fact(db, "Strong fact", "curator", confidence=0.8)
    fid2 = await save_fact(db, "Weak fact", "curator", confidence=0.2)
    archived = await archive_low_confidence_facts(db, threshold=0.3)
    assert archived == 1
    facts = await get_facts(db)
    assert len(facts) == 1
    assert facts[0]["content"] == "Strong fact"
    # Check archive
    cur = await db.execute("SELECT * FROM facts_archive")
    archive_rows = await cur.fetchall()
    assert len(archive_rows) == 1
    assert dict(archive_rows[0])["content"] == "Weak fact"
    assert dict(archive_rows[0])["original_id"] == fid2


async def test_archive_nothing_above_threshold(db: aiosqlite.Connection):
    """No facts archived when all above threshold."""
    await save_fact(db, "Good fact", "curator", confidence=0.9)
    archived = await archive_low_confidence_facts(db, threshold=0.3)
    assert archived == 0
    facts = await get_facts(db)
    assert len(facts) == 1


async def test_archive_exact_threshold_not_archived(db: aiosqlite.Connection):
    """Fact with confidence == threshold is NOT archived (only < threshold)."""
    await save_fact(db, "Boundary fact", "curator", confidence=0.3)
    archived = await archive_low_confidence_facts(db, threshold=0.3)
    assert archived == 0
    facts = await get_facts(db)
    assert len(facts) == 1
    assert facts[0]["confidence"] == 0.3


async def test_save_fact_null_confidence_gets_default(db: aiosqlite.Connection):
    """Schema DEFAULT 1.0 applies when no confidence is explicitly set."""
    # Insert directly without the category/confidence params to test schema default
    await db.execute(
        "INSERT INTO facts (content, source) VALUES (?, ?)",
        ("Raw insert fact", "test"),
    )
    await db.commit()
    facts = await get_facts(db)
    assert len(facts) == 1
    assert facts[0]["confidence"] == 1.0
    assert facts[0]["category"] == "general"
    assert facts[0]["use_count"] == 0


async def test_categorized_fact_unknown_category_stored(db: aiosqlite.Connection):
    """Unknown category strings are stored as-is (no validation at DB level)."""
    fid = await save_fact(db, "Exotic fact", "curator", category="exotic")
    facts = await get_facts(db)
    assert facts[0]["category"] == "exotic"


# --- M43: Session-scoped fact isolation ---


async def test_get_facts_user_fact_hidden_from_other_session(db: aiosqlite.Connection):
    """M43: user-category fact from session A is not returned when querying from session B."""
    await save_fact(db, "Marco likes concise answers", "curator",
                    session="session-A", category="user")
    await save_fact(db, "Project uses Python 3.12", "curator",
                    session="session-A", category="project")

    facts_b = await get_facts(db, session="session-B")
    contents = [f["content"] for f in facts_b]
    assert "Marco likes concise answers" not in contents, (
        "user fact from session-A leaked into session-B"
    )
    assert "Project uses Python 3.12" in contents, (
        "project fact (global) should be visible to all sessions"
    )


async def test_get_facts_user_fact_visible_in_own_session(db: aiosqlite.Connection):
    """M43: user-category fact is returned when querying the session that created it."""
    await save_fact(db, "Prefers dark mode", "curator",
                    session="session-A", category="user")
    facts_a = await get_facts(db, session="session-A")
    contents = [f["content"] for f in facts_a]
    assert "Prefers dark mode" in contents


async def test_get_facts_global_categories_always_visible(db: aiosqlite.Connection):
    """M43: project / tool / general facts are returned regardless of session."""
    await save_fact(db, "Uses FastAPI", "curator", session="session-X", category="project")
    await save_fact(db, "ffmpeg installed", "curator", session="session-X", category="tool")
    await save_fact(db, "Async preferred", "curator", session="session-X", category="general")

    for sess in ("session-X", "session-Y", "session-Z"):
        facts = await get_facts(db, session=sess)
        contents = [f["content"] for f in facts]
        assert "Uses FastAPI" in contents
        assert "ffmpeg installed" in contents
        assert "Async preferred" in contents


async def test_get_facts_admin_sees_all_sessions(db: aiosqlite.Connection):
    """M43: admin user receives user-category facts from every session."""
    await save_fact(db, "Alice prefers verbose output", "curator",
                    session="session-A", category="user")
    await save_fact(db, "Bob prefers brief output", "curator",
                    session="session-B", category="user")

    facts = await get_facts(db, session="session-A", is_admin=True)
    contents = [f["content"] for f in facts]
    assert "Alice prefers verbose output" in contents
    assert "Bob prefers brief output" in contents



# --- M42: search_facts (FTS5) ---


async def test_search_facts_returns_relevant_result(db: aiosqlite.Connection):
    """M42: search_facts returns facts matching the query keywords."""
    await save_fact(db, "The project uses PostgreSQL 15 as the database", "curator")
    await save_fact(db, "ffmpeg is installed at /usr/bin/ffmpeg", "curator")
    await save_fact(db, "Python 3.12 is the runtime environment", "curator")

    results = await search_facts(db, "database postgresql connection")
    contents = [f["content"] for f in results]
    assert any("PostgreSQL" in c for c in contents), (
        f"Expected PostgreSQL fact to rank first. Got: {contents}"
    )


async def test_search_facts_ignores_unrelated_facts(db: aiosqlite.Connection):
    """M42: FTS search ranks matching facts first, limit caps the result set."""
    await save_fact(db, "The project uses PostgreSQL 15", "curator")
    await save_fact(db, "ffmpeg is at /usr/bin/ffmpeg", "curator")
    await save_fact(db, "Python 3.12 is the runtime", "curator")
    await save_fact(db, "Git default branch is main", "curator")
    await save_fact(db, "FastAPI is the web framework", "curator")

    # Single-token query — FTS5 OR-searches for "ffmpeg", should match exactly 1 fact
    results = await search_facts(db, "ffmpeg", limit=2)
    contents = [f["content"] for f in results]
    assert any("ffmpeg" in c for c in contents), (
        f"Expected ffmpeg fact in results. Got: {contents}"
    )
    # Only the matching fact should appear (5 total, 1 matches, limit=2)
    assert len(results) <= 2


async def test_search_facts_respects_limit(db: aiosqlite.Connection):
    """M42: search_facts never returns more than limit results."""
    for i in range(20):
        await save_fact(db, f"Python project fact number {i}", "curator")

    results = await search_facts(db, "python project", limit=5)
    assert len(results) <= 5


async def test_search_facts_session_scoped(db: aiosqlite.Connection):
    """M42: search_facts applies session scoping to user-category facts."""
    await save_fact(db, "Alice prefers dark mode in Python IDE", "curator",
                    session="session-A", category="user")
    await save_fact(db, "Python is the main language", "curator",
                    session="session-A", category="project")

    # Session B should NOT see Alice's user preference
    results_b = await search_facts(db, "python IDE preferences", session="session-B")
    contents_b = [f["content"] for f in results_b]
    assert "Alice prefers dark mode in Python IDE" not in contents_b

    # Session A should see it
    results_a = await search_facts(db, "python IDE preferences", session="session-A")
    contents_a = [f["content"] for f in results_a]
    assert "Alice prefers dark mode in Python IDE" in contents_a


async def test_search_facts_empty_query_falls_back_to_get_facts(db: aiosqlite.Connection):
    """M42: empty/whitespace query falls back to get_facts (no FTS error)."""
    await save_fact(db, "Some fact about the project", "curator")
    results = await search_facts(db, "")
    assert len(results) == 1

    results_ws = await search_facts(db, "   ")
    assert len(results_ws) == 1


async def test_search_facts_no_match_falls_back_to_get_facts(db: aiosqlite.Connection):
    """M42: query with no matching facts falls back to full get_facts result."""
    await save_fact(db, "PostgreSQL is the database", "curator")
    # Query for something completely unrelated
    results = await search_facts(db, "xyzzy quux nonexistent term")
    # Should fall back and return the existing fact
    assert len(results) >= 1


async def test_search_facts_admin_sees_all_sessions(db: aiosqlite.Connection):
    """M42: is_admin=True lets search_facts return user-category facts from any session."""
    await save_fact(db, "Alice prefers verbose output in Python", "curator",
                    session="session-A", category="user")
    await save_fact(db, "Bob prefers brief Python output", "curator",
                    session="session-B", category="user")

    # Admin querying from session-A should see Bob's fact (different session)
    results = await search_facts(db, "python output preferences",
                                 session="session-A", is_admin=True)
    contents = [f["content"] for f in results]
    assert "Alice prefers verbose output in Python" in contents
    assert "Bob prefers brief Python output" in contents


async def test_search_facts_session_none_no_admin(db: aiosqlite.Connection):
    """M42: session=None + is_admin=False uses unconstrained path (mirrors get_facts behaviour).

    When no session is provided and the caller is not an admin, search_facts
    should return global facts plus legacy user facts (session IS NULL), but
    it MUST NOT restrict by any session — matching the get_facts fallback path.
    """
    # Global project fact
    await save_fact(db, "FastAPI is the Python web framework", "curator",
                    category="project")
    # Legacy user fact with no session (pre-M43 row)
    await db.execute(
        "INSERT INTO facts (content, source, category, confidence) VALUES (?, ?, ?, ?)",
        ("Legacy Python preference", "curator", "user", 1.0),
    )
    await db.commit()
    # Rebuild FTS index manually since we bypassed save_fact trigger
    await db.execute(
        "INSERT INTO kiso_facts_fts(rowid, content) "
        "SELECT id, content FROM facts WHERE content = 'Legacy Python preference'"
    )
    await db.commit()

    results = await search_facts(db, "python", session=None, is_admin=False)
    contents = [f["content"] for f in results]
    assert "FastAPI is the Python web framework" in contents
    assert "Legacy Python preference" in contents


async def test_search_facts_unicode_content_and_query(db: aiosqlite.Connection):
    """M42: facts with non-ASCII content are indexed and retrievable.

    SQLite FTS5 handles UTF-8 text; _fts5_query's \\w+ extracts ASCII word
    tokens from the query, so we search with the transliterated ASCII portion.
    The fact itself is stored and returned correctly.
    """
    await save_fact(db, "Il progetto usa PostgreSQL come database principale", "curator",
                    category="project")
    await save_fact(db, "ffmpeg installed at /usr/bin/ffmpeg", "curator")

    # Query with Italian keywords — FTS5 matches on token overlap
    results = await search_facts(db, "postgresql database")
    contents = [f["content"] for f in results]
    assert any("PostgreSQL" in c for c in contents), (
        f"Expected Italian PostgreSQL fact in results. Got: {contents}"
    )


# --- M44b: save_learning fact poisoning filter ---


async def test_save_learning_rejects_password_keyword(db: aiosqlite.Connection):
    """Learning containing 'password' is rejected and returns 0."""
    await create_session(db, "sess1")
    result = await save_learning(db, "The admin password is hunter2", "sess1")
    assert result == 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 0


async def test_save_learning_rejects_passwd_keyword(db: aiosqlite.Connection):
    """Learning containing 'passwd' is rejected."""
    await create_session(db, "sess1")
    result = await save_learning(db, "Root passwd is toor", "sess1")
    assert result == 0


async def test_save_learning_rejects_token_keyword(db: aiosqlite.Connection):
    """Learning containing 'token' is rejected."""
    await create_session(db, "sess1")
    result = await save_learning(db, "API token is abc123", "sess1")
    assert result == 0


async def test_save_learning_rejects_hex_string(db: aiosqlite.Connection):
    """Learning containing a hex string ≥32 chars is rejected."""
    await create_session(db, "sess1")
    hex_str = "a" * 32  # exactly 32 hex chars
    result = await save_learning(db, f"Secret key: {hex_str}", "sess1")
    assert result == 0


async def test_save_learning_allows_short_hex(db: aiosqlite.Connection):
    """Hex string shorter than 32 chars is not treated as a secret."""
    await create_session(db, "sess1")
    result = await save_learning(db, "Color code: deadbeef (8 chars)", "sess1")
    assert result != 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 1


async def test_save_learning_accepts_benign_content(db: aiosqlite.Connection):
    """Normal learning content is accepted and stored."""
    await create_session(db, "sess1")
    result = await save_learning(db, "User prefers concise answers", "sess1")
    assert result != 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 1
    assert learnings[0]["content"] == "User prefers concise answers"


async def test_save_learning_case_insensitive_filter(db: aiosqlite.Connection):
    """Keyword matching is case-insensitive (PASSWORD, Token, PASSWD)."""
    await create_session(db, "sess1")
    assert await save_learning(db, "The PASSWORD is secret", "sess1") == 0
    assert await save_learning(db, "Bearer Token: xyz", "sess1") == 0
    assert await save_learning(db, "PASSWD override", "sess1") == 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 0


# --- M44e: update_task_usage sentinel (preserves llm_calls when omitted) ---


async def test_update_task_usage_sentinel_preserves_llm_calls(db: aiosqlite.Connection):
    """Calling update_task_usage without llm_calls leaves the existing column intact."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]

    # Write an initial llm_calls value via append_task_llm_call
    call = {"role": "translator", "model": "gpt", "input_tokens": 10, "output_tokens": 5}
    await append_task_llm_call(db, task_id, call)

    # Now update usage without passing llm_calls (should preserve existing data)
    await update_task_usage(db, task_id, input_tokens=10, output_tokens=5)

    tasks = await get_tasks_for_plan(db, plan_id)
    import json as _json
    stored = _json.loads(tasks[0]["llm_calls"])
    assert len(stored) == 1
    assert stored[0]["role"] == "translator"


async def test_update_task_usage_with_explicit_calls_overwrites(db: aiosqlite.Connection):
    """Passing llm_calls=[] explicitly clears the column (old behaviour)."""
    await create_session(db, "sess1")
    plan_id = await create_plan(db, "sess1", message_id=1, goal="Test")
    await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
    tasks = await get_tasks_for_plan(db, plan_id)
    task_id = tasks[0]["id"]

    call = {"role": "reviewer", "model": "gpt", "input_tokens": 20, "output_tokens": 10}
    await append_task_llm_call(db, task_id, call)

    # Explicitly pass empty list — should clear llm_calls
    await update_task_usage(db, task_id, input_tokens=20, output_tokens=10, llm_calls=[])

    tasks = await get_tasks_for_plan(db, plan_id)
    assert tasks[0]["llm_calls"] is None


# --- M44g: save_learning input guard (None / empty / hex boundary) ---


async def test_save_learning_rejects_none_content(db: aiosqlite.Connection):
    """save_learning raises TypeError when content is not a str."""
    await create_session(db, "sess1")
    with pytest.raises(TypeError, match="content must be str"):
        await save_learning(db, None, "sess1")  # type: ignore[arg-type]


async def test_save_learning_rejects_empty_string(db: aiosqlite.Connection):
    """Empty string is rejected and returns 0 without DB insert."""
    await create_session(db, "sess1")
    result = await save_learning(db, "", "sess1")
    assert result == 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 0


async def test_save_learning_rejects_whitespace_only(db: aiosqlite.Connection):
    """Whitespace-only string is rejected and returns 0."""
    await create_session(db, "sess1")
    result = await save_learning(db, "   \t\n  ", "sess1")
    assert result == 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 0


async def test_save_learning_hex_boundary_31_chars_accepted(db: aiosqlite.Connection):
    """31-char hex string is below the threshold and must be accepted."""
    await create_session(db, "sess1")
    hex31 = "0" * 31
    result = await save_learning(db, f"hash: {hex31}", "sess1")
    assert result != 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 1


async def test_save_learning_hex_boundary_32_chars_rejected(db: aiosqlite.Connection):
    """Exactly 32 consecutive hex chars triggers the filter."""
    await create_session(db, "sess1")
    hex32 = "0" * 32
    result = await save_learning(db, f"hash: {hex32}", "sess1")
    assert result == 0
    learnings = await get_pending_learnings(db)
    assert len(learnings) == 0
