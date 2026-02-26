"""SQLite storage layer — module-level async functions."""

from __future__ import annotations

import json
import logging
import re
import aiosqlite
from pathlib import Path

log = logging.getLogger(__name__)

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
    llm_calls           TEXT,
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
    substatus    TEXT,
    output       TEXT,
    stderr       TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    review_verdict  TEXT,
    review_reason   TEXT,
    review_learning TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    llm_calls     TEXT,
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
    category   TEXT DEFAULT 'general',
    confidence REAL DEFAULT 1.0,
    last_used  TEXT,
    use_count  INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS kiso_facts_fts USING fts5(
    content,
    content='facts',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS facts_fts_insert AFTER INSERT ON facts BEGIN
    INSERT INTO kiso_facts_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_update AFTER UPDATE ON facts BEGIN
    INSERT INTO kiso_facts_fts(kiso_facts_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO kiso_facts_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_delete AFTER DELETE ON facts BEGIN
    INSERT INTO kiso_facts_fts(kiso_facts_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TABLE IF NOT EXISTS facts_archive (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    original_id INTEGER,
    content     TEXT NOT NULL,
    source      TEXT NOT NULL,
    session     TEXT,
    category    TEXT DEFAULT 'general',
    confidence  REAL,
    last_used   TEXT,
    use_count   INTEGER DEFAULT 0,
    archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at  DATETIME
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

"""


async def _rows_to_dicts(cur: aiosqlite.Cursor) -> list[dict]:
    """Fetch all rows from a cursor and return as a list of dicts."""
    return [dict(r) for r in await cur.fetchall()]


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
        ("tasks", "llm_calls", "ALTER TABLE tasks ADD COLUMN llm_calls TEXT"),
        ("plans", "llm_calls", "ALTER TABLE plans ADD COLUMN llm_calls TEXT"),
        ("tasks", "substatus", "ALTER TABLE tasks ADD COLUMN substatus TEXT"),
        ("tasks", "retry_count", "ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"),
        ("facts", "category", "ALTER TABLE facts ADD COLUMN category TEXT DEFAULT 'general'"),
        ("facts", "confidence", "ALTER TABLE facts ADD COLUMN confidence REAL DEFAULT 1.0"),
        ("facts", "last_used", "ALTER TABLE facts ADD COLUMN last_used TEXT"),
        ("facts", "use_count", "ALTER TABLE facts ADD COLUMN use_count INTEGER DEFAULT 0"),
    ]
    _known = frozenset(t for t, _, _ in migrations)
    for table, column, sql in migrations:
        assert table in _known, f"Unknown table in migration: {table!r}"
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(sql)
    await db.commit()

    # Rebuild FTS index for pre-existing facts (first run after M42).
    # The triggers keep the index current for new rows; existing rows need a
    # one-time backfill.  We check by comparing counts — if the FTS index is
    # empty while facts exist, it has not been populated yet.
    try:
        cur = await db.execute("SELECT count(*) FROM kiso_facts_fts")
        fts_count = (await cur.fetchone())[0]
        if fts_count == 0:
            cur = await db.execute("SELECT count(*) FROM facts")
            facts_count = (await cur.fetchone())[0]
            if facts_count > 0:
                await db.execute(
                    "INSERT INTO kiso_facts_fts(rowid, content) SELECT id, content FROM facts"
                )
                await db.commit()
    except Exception:
        pass  # FTS5 not compiled into this SQLite build — graceful degradation


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
    return await _rows_to_dicts(cur)


async def get_sessions_for_user(db: aiosqlite.Connection, username: str) -> list[dict]:
    """Return sessions where user has sent messages."""
    cur = await db.execute(
        "SELECT DISTINCT s.session, s.connector, s.description, s.updated_at "
        "FROM sessions s JOIN messages m ON s.session = m.session "
        "WHERE m.user = ? ORDER BY s.updated_at DESC",
        (username,),
    )
    return await _rows_to_dicts(cur)


async def get_all_sessions(db: aiosqlite.Connection) -> list[dict]:
    """Return all sessions."""
    cur = await db.execute(
        "SELECT session, connector, description, updated_at "
        "FROM sessions ORDER BY updated_at DESC"
    )
    return await _rows_to_dicts(cur)


async def get_tasks_for_session(
    db: aiosqlite.Connection, session: str, after: int = 0
) -> list[dict]:
    """Return tasks for a session, optionally after a given id."""
    cur = await db.execute(
        "SELECT * FROM tasks WHERE session = ? AND id > ? ORDER BY id",
        (session, after),
    )
    return await _rows_to_dicts(cur)


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
    rows = await _rows_to_dicts(cur)
    rows.reverse()
    return rows


async def get_facts(
    db: aiosqlite.Connection,
    *,
    session: str | None = None,
    is_admin: bool = False,
) -> list[dict]:
    """Return facts filtered by session scope (M43).

    - project / tool / general facts are always global and returned unconditionally.
    - user-category facts are visible only in the session where they were created.
    - Facts with ``session IS NULL`` are legacy global facts and are always included.
    - Admin users bypass all filtering and receive every fact.

    When *session* is ``None`` and *is_admin* is ``False`` the query returns all
    non-user facts plus user facts with no session (legacy global rows).  This
    preserves backward-compatible behaviour for callers that do not yet supply a
    session (e.g. system operations like fact consolidation that use ``is_admin=True``
    explicitly).
    """
    if is_admin or session is None:
        cur = await db.execute("SELECT * FROM facts ORDER BY id")
    else:
        cur = await db.execute(
            "SELECT * FROM facts "
            "WHERE category != 'user' OR session IS NULL OR session = ? "
            "ORDER BY id",
            (session,),
        )
    return await _rows_to_dicts(cur)


def _fts5_query(text: str) -> str:
    """Extract plain word tokens from text for use as an FTS5 MATCH query.

    Strips FTS5 special characters (quotes, parentheses, operators) so
    arbitrary user messages can be passed safely without causing parse errors.
    """
    return " ".join(re.findall(r"\w+", text))


async def search_facts(
    db: aiosqlite.Connection,
    query: str,
    *,
    session: str | None = None,
    is_admin: bool = False,
    limit: int = 15,
) -> list[dict]:
    """Return up to *limit* facts most relevant to *query* (FTS5 BM25 ranking).

    Session scoping mirrors :func:`get_facts` — user-category facts are
    filtered to the current session unless *is_admin* is ``True``.

    Falls back to :func:`get_facts` when:
    - FTS5 is not compiled into the SQLite build
    - *query* contains no searchable tokens
    - The FTS search returns no results (ensures context is always present)
    """
    fts_q = _fts5_query(query)
    if not fts_q:
        return await get_facts(db, session=session, is_admin=is_admin)

    try:
        if is_admin or session is None:
            cur = await db.execute(
                "SELECT f.* FROM facts f "
                "JOIN kiso_facts_fts fts ON fts.rowid = f.id "
                "WHERE kiso_facts_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_q, limit),
            )
        else:
            cur = await db.execute(
                "SELECT f.* FROM facts f "
                "JOIN kiso_facts_fts fts ON fts.rowid = f.id "
                "WHERE kiso_facts_fts MATCH ? "
                "AND (f.category != 'user' OR f.session IS NULL OR f.session = ?) "
                "ORDER BY rank LIMIT ?",
                (fts_q, session, limit),
            )
        results = await _rows_to_dicts(cur)
    except Exception:
        return await get_facts(db, session=session, is_admin=is_admin)

    # If FTS found no matches, fall back to the full filtered set so the planner
    # always has some knowledge context (avoids silent empty-facts scenario).
    if not results:
        return await get_facts(db, session=session, is_admin=is_admin)
    return results


async def get_pending_items(db: aiosqlite.Connection, session: str) -> list[dict]:
    """Return open pending items (global + session-scoped)."""
    cur = await db.execute(
        "SELECT * FROM pending WHERE status = 'open' "
        "AND (scope = 'global' OR scope = ?) ORDER BY id",
        (session,),
    )
    return await _rows_to_dicts(cur)


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


_KEEP_LLM_CALLS = object()  # sentinel: don't touch the llm_calls column


async def update_task_usage(
    db: aiosqlite.Connection,
    task_id: int,
    input_tokens: int,
    output_tokens: int,
    llm_calls: list[dict] | None | object = _KEEP_LLM_CALLS,
) -> None:
    """Store per-step token usage on a task.

    When *llm_calls* is omitted the existing ``llm_calls`` column is
    preserved (only token totals are updated).  Pass an explicit list to
    overwrite the column, or ``None`` to clear it.
    """
    if llm_calls is _KEEP_LLM_CALLS:
        await db.execute(
            "UPDATE tasks SET input_tokens = ?, output_tokens = ? WHERE id = ?",
            (input_tokens, output_tokens, task_id),
        )
    else:
        calls_json = json.dumps(llm_calls) if llm_calls else None
        await db.execute(
            "UPDATE tasks SET input_tokens = ?, output_tokens = ?, llm_calls = ? WHERE id = ?",
            (input_tokens, output_tokens, calls_json, task_id),
        )
    await db.commit()


async def update_task_substatus(
    db: aiosqlite.Connection, task_id: int, substatus: str
) -> None:
    """Update only the substatus text (lightweight, no output/status change)."""
    await db.execute(
        "UPDATE tasks SET substatus = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (substatus, task_id),
    )
    await db.commit()


async def update_task_retry_count(
    db: aiosqlite.Connection, task_id: int, retry_count: int
) -> None:
    """Update the retry_count on a task."""
    await db.execute(
        "UPDATE tasks SET retry_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (retry_count, task_id),
    )
    await db.commit()


async def append_task_llm_call(
    db: aiosqlite.Connection, task_id: int, call_data: dict
) -> None:
    """Append a single LLM call entry to the task's llm_calls JSON array."""
    cur = await db.execute(
        "SELECT llm_calls FROM tasks WHERE id = ?", (task_id,),
    )
    row = await cur.fetchone()
    try:
        existing = json.loads(row["llm_calls"]) if row and row["llm_calls"] else []
    except (json.JSONDecodeError, TypeError):
        existing = []
    existing.append(call_data)
    await db.execute(
        "UPDATE tasks SET llm_calls = ? WHERE id = ?",
        (json.dumps(existing), task_id),
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
    llm_calls: list[dict] | None | object = _KEEP_LLM_CALLS,
) -> None:
    """Store accumulated token usage on a plan.

    When *llm_calls* is omitted the existing ``llm_calls`` column is
    preserved (only totals and model are updated).  Pass an explicit list
    to overwrite the column, or ``None`` to clear it.
    """
    if llm_calls is _KEEP_LLM_CALLS:
        await db.execute(
            "UPDATE plans SET total_input_tokens = ?, total_output_tokens = ?, model = ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, model, plan_id),
        )
    else:
        calls_json = json.dumps(llm_calls) if llm_calls else None
        await db.execute(
            "UPDATE plans SET total_input_tokens = ?, total_output_tokens = ?, model = ?, llm_calls = ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, model, calls_json, plan_id),
        )
    await db.commit()


async def get_tasks_for_plan(db: aiosqlite.Connection, plan_id: int) -> list[dict]:
    """Return all tasks for a plan, ordered by id."""
    cur = await db.execute(
        "SELECT * FROM tasks WHERE plan_id = ? ORDER BY id", (plan_id,)
    )
    return await _rows_to_dicts(cur)

# Patterns that indicate secret-like content (passwords, tokens, hex keys).
# Matched case-insensitively; learnings matching any pattern are rejected.
_SENSITIVE_PATTERN = re.compile(
    r"\b(password|passwd|token)\b"
    r"|[0-9a-fA-F]{32,}",
    re.IGNORECASE,
)


async def save_learning(
    db: aiosqlite.Connection,
    content: str,
    session: str,
    user: str | None = None,
) -> int:
    """Insert a learning row. Returns learning id, or 0 if content was rejected.

    Learnings are rejected (return 0) when:
    - *content* is empty or whitespace-only
    - *content* matches secret-like patterns (password/passwd/token keywords,
      hex strings ≥ 32 chars) — logged as a warning to prevent fact poisoning

    Raises ``TypeError`` if *content* is not a ``str``.
    """
    if not isinstance(content, str):
        raise TypeError(
            f"save_learning: content must be str, got {type(content).__name__!r}"
        )
    if not content.strip():
        return 0
    if _SENSITIVE_PATTERN.search(content):
        log.warning(
            "Learning rejected (contains secret-like content): %s", content[:80]
        )
        return 0
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
    return await _rows_to_dicts(cur)


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
    category: str = "general",
    confidence: float = 1.0,
) -> int:
    """Insert a fact row. Returns fact id."""
    cur = await db.execute(
        "INSERT INTO facts (content, source, session, category, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (content, source, session, category, confidence),
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
    return await _rows_to_dicts(cur)


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
    return await _rows_to_dicts(cur)


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
    return await _rows_to_dicts(cur)


async def update_fact_usage(
    db: aiosqlite.Connection, fact_ids: list[int]
) -> None:
    """Bump use_count and last_used for the given fact IDs. No-op if list is empty."""
    if not fact_ids:
        return
    placeholders = ",".join("?" for _ in fact_ids)
    await db.execute(
        f"UPDATE facts SET use_count = use_count + 1, "
        f"last_used = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
        fact_ids,
    )
    await db.commit()


async def decay_facts(
    db: aiosqlite.Connection,
    decay_days: int = 7,
    decay_rate: float = 0.1,
) -> int:
    """Reduce confidence of facts not used in decay_days. Returns rows affected."""
    cur = await db.execute(
        "UPDATE facts SET confidence = MAX(0.0, confidence - ?) "
        "WHERE COALESCE(last_used, created_at) < datetime('now', ?)",
        (decay_rate, f"-{decay_days} days"),
    )
    await db.commit()
    return cur.rowcount


async def archive_low_confidence_facts(
    db: aiosqlite.Connection, threshold: float = 0.3
) -> int:
    """Copy facts with confidence < threshold to facts_archive, then delete. Returns rows archived."""
    cur = await db.execute(
        "INSERT INTO facts_archive (original_id, content, source, session, "
        "category, confidence, last_used, use_count, created_at) "
        "SELECT id, content, source, session, category, confidence, "
        "last_used, use_count, created_at FROM facts WHERE confidence < ?",
        (threshold,),
    )
    archived = cur.rowcount
    if archived:
        await db.execute("DELETE FROM facts WHERE confidence < ?", (threshold,))
    await db.commit()
    return archived


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


