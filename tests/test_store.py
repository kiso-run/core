"""Tests for kiso/store.py."""

from __future__ import annotations

import aiosqlite

from kiso.store import (
    count_messages,
    create_plan,
    create_session,
    create_task,
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
    update_learning,
    update_plan_status,
    update_plan_usage,
    update_summary,
    update_task,
    update_task_command,
    update_task_review,
    update_task_usage,
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
