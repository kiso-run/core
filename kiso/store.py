"""SQLite storage layer — module-level async functions."""

from __future__ import annotations

import uuid

import aiosqlite
from pathlib import Path

SCHEMA = """\
CREATE TABLE IF NOT EXISTS sessions (
    session     TEXT PRIMARY KEY,
    connector   TEXT,
    webhook     TEXT,
    description TEXT,
    summary     TEXT DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session   TEXT NOT NULL,
    user      TEXT,
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    trusted   BOOLEAN DEFAULT 1,
    processed BOOLEAN DEFAULT 0,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session, id);
CREATE INDEX IF NOT EXISTS idx_messages_unprocessed ON messages(processed) WHERE processed = 0;

CREATE TABLE IF NOT EXISTS plans (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session             TEXT NOT NULL,
    message_id          INTEGER NOT NULL,
    parent_id           INTEGER,
    goal                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'running',
    total_input_tokens  INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    model               TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plans_session ON plans(session, id);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL,
    session      TEXT NOT NULL,
    type         TEXT NOT NULL,
    detail       TEXT NOT NULL,
    skill        TEXT,
    args         TEXT,
    expect       TEXT,
    command      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    output       TEXT,
    stderr       TEXT,
    review_verdict  TEXT,
    review_reason   TEXT,
    review_learning TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tasks_plan ON tasks(plan_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(session, status);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL,
    session    TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS learnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    session    TEXT NOT NULL,
    user       TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    scope      TEXT NOT NULL,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pending_scope ON pending(scope, status);

CREATE TABLE IF NOT EXISTS published (
    id         TEXT PRIMARY KEY,
    session    TEXT NOT NULL,
    filename   TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


async def init_db(db_path: Path) -> aiosqlite.Connection:
    """Create tables, enable WAL, set row_factory, return connection."""
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()

    # --- Migrations for existing databases ---
    await _migrate(db)

    return db


async def _migrate(db: aiosqlite.Connection) -> None:
    """Add columns that may be missing from older schemas."""
    migrations = [
        ("tasks", "command", "ALTER TABLE tasks ADD COLUMN command TEXT"),
        ("tasks", "input_tokens", "ALTER TABLE tasks ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0"),
        ("tasks", "output_tokens", "ALTER TABLE tasks ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0"),
        ("plans", "total_input_tokens", "ALTER TABLE plans ADD COLUMN total_input_tokens INTEGER NOT NULL DEFAULT 0"),
        ("plans", "total_output_tokens", "ALTER TABLE plans ADD COLUMN total_output_tokens INTEGER NOT NULL DEFAULT 0"),
        ("plans", "model", "ALTER TABLE plans ADD COLUMN model TEXT"),
    ]
    for table, column, sql in migrations:
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(sql)
    await db.commit()


async def get_session(db: aiosqlite.Connection, session: str) -> dict | None:
    """Return session row as dict, or None."""
    cur = await db.execute("SELECT * FROM sessions WHERE session = ?", (session,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def create_session(
    db: aiosqlite.Connection,
    session: str,
    connector: str | None = None,
    webhook: str | None = None,
    description: str | None = None,
) -> dict:
    """Create a session if it doesn't exist (idempotent). Return session dict."""
    existing = await get_session(db, session)
    if existing:
        return existing
    await db.execute(
        "INSERT INTO sessions (session, connector, webhook, description) VALUES (?, ?, ?, ?)",
        (session, connector, webhook, description),
    )
    await db.commit()
    return (await get_session(db, session))  # type: ignore[return-value]


async def upsert_session(
    db: aiosqlite.Connection,
    session: str,
    connector: str | None = None,
    webhook: str | None = None,
    description: str | None = None,
) -> tuple[dict, bool]:
    """Create or update a session. Returns (session_dict, created).

    If session exists: update connector, webhook, description, updated_at.
    If not: insert new row.
    """
    existing = await get_session(db, session)
    if existing:
        await db.execute(
            "UPDATE sessions SET connector = ?, webhook = ?, description = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE session = ?",
            (connector, webhook, description, session),
        )
        await db.commit()
        return (await get_session(db, session)), False  # type: ignore[return-value]
    await db.execute(
        "INSERT INTO sessions (session, connector, webhook, description) VALUES (?, ?, ?, ?)",
        (session, connector, webhook, description),
    )
    await db.commit()
    return (await get_session(db, session)), True  # type: ignore[return-value]


async def save_message(
    db: aiosqlite.Connection,
    session: str,
    user: str | None,
    role: str,
    content: str,
    trusted: bool = True,
    processed: bool = False,
) -> int:
    """Insert a message row. Bumps session updated_at. Returns message id."""
    cur = await db.execute(
        "INSERT INTO messages (session, user, role, content, trusted, processed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session, user, role, content, trusted, processed),
    )
    msg_id = cur.lastrowid
    await db.execute(
        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE session = ?",
        (session,),
    )
    await db.commit()
    return msg_id  # type: ignore[return-value]


async def mark_message_processed(db: aiosqlite.Connection, msg_id: int) -> None:
    """Set processed=1 for a message."""
    await db.execute("UPDATE messages SET processed = 1 WHERE id = ?", (msg_id,))
    await db.commit()


async def get_unprocessed_messages(db: aiosqlite.Connection) -> list[dict]:
    """Return all unprocessed messages (processed=0)."""
    cur = await db.execute(
        "SELECT * FROM messages WHERE processed = 0 ORDER BY id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_sessions_for_user(db: aiosqlite.Connection, username: str) -> list[dict]:
    """Return sessions where user has sent messages."""
    cur = await db.execute(
        "SELECT DISTINCT s.session, s.connector, s.description, s.updated_at "
        "FROM sessions s JOIN messages m ON s.session = m.session "
        "WHERE m.user = ? ORDER BY s.updated_at DESC",
        (username,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_all_sessions(db: aiosqlite.Connection) -> list[dict]:
    """Return all sessions."""
    cur = await db.execute(
        "SELECT session, connector, description, updated_at "
        "FROM sessions ORDER BY updated_at DESC"
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_tasks_for_session(
    db: aiosqlite.Connection, session: str, after: int = 0
) -> list[dict]:
    """Return tasks for a session, optionally after a given id."""
    cur = await db.execute(
        "SELECT * FROM tasks WHERE session = ? AND id > ? ORDER BY id",
        (session, after),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_plan_for_session(db: aiosqlite.Connection, session: str) -> dict | None:
    """Return the latest plan for a session, or None."""
    cur = await db.execute(
        "SELECT * FROM plans WHERE session = ? ORDER BY id DESC LIMIT 1",
        (session,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_recent_messages(
    db: aiosqlite.Connection, session: str, limit: int = 5
) -> list[dict]:
    """Return the most recent messages for a session (trusted only), oldest first."""
    cur = await db.execute(
        "SELECT * FROM messages WHERE session = ? AND trusted = 1 "
        "ORDER BY id DESC LIMIT ?",
        (session, limit),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    rows.reverse()
    return rows


async def get_facts(db: aiosqlite.Connection) -> list[dict]:
    """Return all facts (global)."""
    cur = await db.execute("SELECT * FROM facts ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


async def get_pending_items(db: aiosqlite.Connection, session: str) -> list[dict]:
    """Return open pending items (global + session-scoped)."""
    cur = await db.execute(
        "SELECT * FROM pending WHERE status = 'open' "
        "AND (scope = 'global' OR scope = ?) ORDER BY id",
        (session,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def create_plan(
    db: aiosqlite.Connection,
    session: str,
    message_id: int,
    goal: str,
    parent_id: int | None = None,
) -> int:
    """Insert a plan row. Returns plan id."""
    cur = await db.execute(
        "INSERT INTO plans (session, message_id, goal, parent_id) VALUES (?, ?, ?, ?)",
        (session, message_id, goal, parent_id),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def update_task(
    db: aiosqlite.Connection,
    task_id: int,
    status: str,
    output: str | None = None,
    stderr: str | None = None,
) -> None:
    """Update task status, output, stderr, and updated_at."""
    await db.execute(
        "UPDATE tasks SET status = ?, output = ?, stderr = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, output, stderr, task_id),
    )
    await db.commit()


async def update_task_review(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: str,
    reason: str | None = None,
    learning: str | None = None,
) -> None:
    """Persist review verdict on a task row."""
    await db.execute(
        "UPDATE tasks SET review_verdict=?, review_reason=?, review_learning=? WHERE id=?",
        (verdict, reason, learning, task_id),
    )
    await db.commit()


async def update_task_command(
    db: aiosqlite.Connection, task_id: int, command: str
) -> None:
    """Set the translated shell command on a task."""
    await db.execute(
        "UPDATE tasks SET command = ? WHERE id = ?", (command, task_id)
    )
    await db.commit()


async def update_task_usage(
    db: aiosqlite.Connection, task_id: int, input_tokens: int, output_tokens: int
) -> None:
    """Store per-step token usage on a task."""
    await db.execute(
        "UPDATE tasks SET input_tokens = ?, output_tokens = ? WHERE id = ?",
        (input_tokens, output_tokens, task_id),
    )
    await db.commit()


async def update_plan_status(
    db: aiosqlite.Connection, plan_id: int, status: str
) -> None:
    """Update plan status."""
    await db.execute(
        "UPDATE plans SET status = ? WHERE id = ?", (status, plan_id)
    )
    await db.commit()


async def update_plan_usage(
    db: aiosqlite.Connection,
    plan_id: int,
    input_tokens: int,
    output_tokens: int,
    model: str | None = None,
) -> None:
    """Store accumulated token usage on a plan."""
    await db.execute(
        "UPDATE plans SET total_input_tokens = ?, total_output_tokens = ?, model = ? "
        "WHERE id = ?",
        (input_tokens, output_tokens, model, plan_id),
    )
    await db.commit()


async def get_tasks_for_plan(db: aiosqlite.Connection, plan_id: int) -> list[dict]:
    """Return all tasks for a plan, ordered by id."""
    cur = await db.execute(
        "SELECT * FROM tasks WHERE plan_id = ? ORDER BY id", (plan_id,)
    )
    return [dict(r) for r in await cur.fetchall()]

async def save_learning(
    db: aiosqlite.Connection,
    content: str,
    session: str,
    user: str | None = None,
) -> int:
    """Insert a learning row. Returns learning id."""
    cur = await db.execute(
        "INSERT INTO learnings (content, session, user) VALUES (?, ?, ?)",
        (content, session, user),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def get_pending_learnings(
    db: aiosqlite.Connection, limit: int = 50
) -> list[dict]:
    """Return pending learnings, oldest first."""
    cur = await db.execute(
        "SELECT * FROM learnings WHERE status = 'pending' ORDER BY id LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def update_learning(
    db: aiosqlite.Connection, learning_id: int, status: str
) -> None:
    """Set learning status (promoted or discarded)."""
    await db.execute(
        "UPDATE learnings SET status = ? WHERE id = ?", (status, learning_id)
    )
    await db.commit()


async def save_fact(
    db: aiosqlite.Connection,
    content: str,
    source: str,
    session: str | None = None,
) -> int:
    """Insert a fact row. Returns fact id."""
    cur = await db.execute(
        "INSERT INTO facts (content, source, session) VALUES (?, ?, ?)",
        (content, source, session),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def save_pending_item(
    db: aiosqlite.Connection,
    content: str,
    scope: str,
    source: str,
) -> int:
    """Insert a pending item row. Returns pending id."""
    cur = await db.execute(
        "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
        (content, scope, source),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def update_summary(
    db: aiosqlite.Connection, session: str, summary: str
) -> None:
    """Update session summary."""
    await db.execute(
        "UPDATE sessions SET summary = ?, updated_at = CURRENT_TIMESTAMP WHERE session = ?",
        (summary, session),
    )
    await db.commit()


async def count_messages(db: aiosqlite.Connection, session: str) -> int:
    """Count trusted messages for a session."""
    cur = await db.execute(
        "SELECT COUNT(*) FROM messages WHERE session = ? AND trusted = 1",
        (session,),
    )
    row = await cur.fetchone()
    return row[0]


async def get_oldest_messages(
    db: aiosqlite.Connection, session: str, limit: int
) -> list[dict]:
    """Return oldest trusted messages for a session."""
    cur = await db.execute(
        "SELECT * FROM messages WHERE session = ? AND trusted = 1 "
        "ORDER BY id ASC LIMIT ?",
        (session, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def delete_facts(db: aiosqlite.Connection, fact_ids: list[int]) -> None:
    """Delete facts by id."""
    if not fact_ids:
        return
    placeholders = ",".join("?" for _ in fact_ids)
    await db.execute(f"DELETE FROM facts WHERE id IN ({placeholders})", fact_ids)
    await db.commit()


async def get_untrusted_messages(
    db: aiosqlite.Connection, session: str, limit: int = 20
) -> list[dict]:
    """Return untrusted messages for a session, oldest first."""
    cur = await db.execute(
        "SELECT * FROM messages WHERE session = ? AND trusted = 0 "
        "ORDER BY id ASC LIMIT ?",
        (session, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def recover_stale_running(db: aiosqlite.Connection) -> tuple[int, int]:
    """Mark stale running plans/tasks as failed after server restart.

    Returns (plans_count, tasks_count).
    """
    cur = await db.execute(
        "UPDATE plans SET status = 'failed' WHERE status = 'running'"
    )
    plans_count = cur.rowcount
    cur = await db.execute(
        "UPDATE tasks SET status = 'failed', output = 'Server restarted' "
        "WHERE status = 'running'"
    )
    tasks_count = cur.rowcount
    await db.commit()
    return plans_count, tasks_count


async def get_unprocessed_trusted_messages(db: aiosqlite.Connection) -> list[dict]:
    """Return unprocessed trusted messages, ordered by id."""
    cur = await db.execute(
        "SELECT * FROM messages WHERE processed = 0 AND trusted = 1 ORDER BY id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def create_task(
    db: aiosqlite.Connection,
    plan_id: int,
    session: str,
    type: str,
    detail: str,
    skill: str | None = None,
    args: str | None = None,
    expect: str | None = None,
) -> int:
    """Insert a task row. Returns task id."""
    cur = await db.execute(
        "INSERT INTO tasks (plan_id, session, type, detail, skill, args, expect) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (plan_id, session, type, detail, skill, args, expect),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def publish_file(
    db: aiosqlite.Connection,
    session: str,
    filename: str,
    path: str,
) -> str:
    """Insert a published file entry. Returns the UUID4 id."""
    file_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO published (id, session, filename, path) VALUES (?, ?, ?, ?)",
        (file_id, session, filename, path),
    )
    await db.commit()
    return file_id


async def get_published_file(db: aiosqlite.Connection, file_id: str) -> dict | None:
    """Look up a published file by UUID. Returns dict or None."""
    cur = await db.execute("SELECT * FROM published WHERE id = ?", (file_id,))
    row = await cur.fetchone()
    return dict(row) if row else None
