"""SQLite storage layer — module-level async functions."""

from __future__ import annotations

import json
import logging
import re
from typing import TypedDict
import aiosqlite
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed dicts for store entities (returned as plain dicts from aiosqlite rows)
# ---------------------------------------------------------------------------

class SessionDict(TypedDict):
    session: str
    connector: str | None
    webhook: str | None
    description: str | None
    summary: str
    created_at: str
    updated_at: str


class MessageDict(TypedDict):
    id: int
    session: str
    user: str | None
    role: str
    content: str
    trusted: bool
    processed: bool
    timestamp: str


class PlanDict(TypedDict):
    id: int
    session: str
    message_id: int
    parent_id: int | None
    goal: str
    status: str
    total_input_tokens: int
    total_output_tokens: int
    model: str | None
    llm_calls: str | None
    created_at: str


class TaskDict(TypedDict):
    id: int
    plan_id: int
    session: str
    type: str
    detail: str
    skill: str | None
    args: str | None
    expect: str | None
    command: str | None
    status: str
    substatus: str | None
    output: str | None
    stderr: str | None
    retry_count: int
    review_verdict: str | None
    review_reason: str | None
    review_learning: str | None
    input_tokens: int
    output_tokens: int
    llm_calls: str | None
    duration_ms: int | None
    created_at: str
    updated_at: str


class FactDict(TypedDict):
    id: int
    content: str
    source: str
    session: str | None
    category: str
    confidence: float
    last_used: str | None
    use_count: int
    created_at: str


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
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user);
CREATE INDEX IF NOT EXISTS idx_messages_session_user ON messages(session, user);

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
    install_proposal    BOOLEAN DEFAULT 0,
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
    duration_ms INTEGER DEFAULT NULL,
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
CREATE INDEX IF NOT EXISTS idx_facts_cat_sess ON facts(category, session);

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

CREATE TABLE IF NOT EXISTS fact_tags (
    fact_id INTEGER NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (fact_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_fact_tags_tag ON fact_tags(tag);
CREATE TRIGGER IF NOT EXISTS fact_tags_cleanup AFTER DELETE ON facts BEGIN
    DELETE FROM fact_tags WHERE fact_id = old.id;
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

CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    scope      TEXT NOT NULL,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pending_scope ON pending(scope, status);

CREATE TABLE IF NOT EXISTS cron_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    schedule    TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    enabled     BOOLEAN DEFAULT 1,
    last_run    TEXT,
    next_run    TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cron_jobs_enabled ON cron_jobs(enabled, next_run);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    description TEXT,
    created_by  TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('member', 'viewer')),
    PRIMARY KEY (project_id, username)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

"""


async def _rows_to_dicts(cur: aiosqlite.Cursor) -> list[dict]:
    """Fetch all rows from a cursor and return as a list of dicts."""
    return [dict(r) for r in await cur.fetchall()]


async def _update_field(
    db: aiosqlite.Connection,
    table: str,
    field: str,
    value: object,
    row_id: object,
    *,
    id_column: str = "id",
    update_timestamp: bool = False,
) -> None:
    """Generic single-field UPDATE helper.

    Sets *field* = *value* on the row identified by *id_column* = *row_id*.
    When *update_timestamp* is True, also sets ``updated_at = CURRENT_TIMESTAMP``.
    """
    if update_timestamp:
        sql = (
            f"UPDATE {table} SET {field} = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE {id_column} = ?"
        )
    else:
        sql = f"UPDATE {table} SET {field} = ? WHERE {id_column} = ?"
    await db.execute(sql, (value, row_id))
    await db.commit()


async def _update_fields(
    db: aiosqlite.Connection,
    table: str,
    fields: dict[str, object],
    row_id: object,
    *,
    id_column: str = "id",
) -> None:
    """Generic multi-field UPDATE helper.

    Sets each key=value from *fields*, plus ``updated_at = CURRENT_TIMESTAMP``,
    on the row identified by *id_column* = *row_id*.
    """
    set_clauses = [f"{k} = ?" for k in fields]
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {id_column} = ?"
    await db.execute(sql, (*fields.values(), row_id))
    await db.commit()


async def init_db(db_path: Path) -> aiosqlite.Connection:
    """Create tables, enable WAL, set busy_timeout, set row_factory, return connection."""
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    # Prevent SQLITE_BUSY errors when concurrent coroutines commit close together.
    await db.execute("PRAGMA busy_timeout = 5000")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()

    # --- Migrations for existing databases ---
    cur = await db.execute("PRAGMA table_info(tasks)")
    existing_cols = {row[1] for row in await cur.fetchall()}
    if "duration_ms" not in existing_cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN duration_ms INTEGER DEFAULT NULL")
        await db.commit()
    if "parallel_group" not in existing_cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN parallel_group INTEGER")
        await db.commit()
    if "review_learning_tags" not in existing_cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN review_learning_tags TEXT")
        await db.commit()

    # add project_id to sessions and facts tables
    cur = await db.execute("PRAGMA table_info(sessions)")
    sess_cols = {row[1] for row in await cur.fetchall()}
    if "project_id" not in sess_cols:
        await db.execute("ALTER TABLE sessions ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        await db.commit()
    cur = await db.execute("PRAGMA table_info(facts)")
    fact_cols_684 = {row[1] for row in await cur.fetchall()}
    if "project_id" not in fact_cols_684:
        await db.execute("ALTER TABLE facts ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        await db.commit()

    # add source column to messages table
    cur = await db.execute("PRAGMA table_info(messages)")
    msg_cols = {row[1] for row in await cur.fetchall()}
    if "source" not in msg_cols:
        await db.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'user'")
        await db.commit()

    # add install_proposal to plans table
    cur = await db.execute("PRAGMA table_info(plans)")
    plan_cols = {row[1] for row in await cur.fetchall()}
    if "install_proposal" not in plan_cols:
        await db.execute(
            "ALTER TABLE plans ADD COLUMN install_proposal BOOLEAN DEFAULT 0"
        )
        await db.commit()

    # add entity_id to facts table
    cur = await db.execute("PRAGMA table_info(facts)")
    fact_cols = {row[1] for row in await cur.fetchall()}
    if "entity_id" not in fact_cols:
        await db.execute(
            "ALTER TABLE facts ADD COLUMN entity_id INTEGER REFERENCES entities(id)"
        )
        await db.commit()

    # migrate entity: tags to entity records
    cur = await db.execute("SELECT DISTINCT tag FROM fact_tags WHERE tag LIKE 'entity:%'")
    entity_tags = await cur.fetchall()
    for row in entity_tags:
        tag = row[0]
        name = tag[len("entity:"):]
        entity_id = await find_or_create_entity(db, name, "tool")
        await db.execute(
            "UPDATE facts SET entity_id = ? WHERE id IN "
            "(SELECT fact_id FROM fact_tags WHERE tag = ?)",
            (entity_id, tag),
        )
        await db.execute("DELETE FROM fact_tags WHERE tag = ?", (tag,))
    if entity_tags:
        await db.commit()

    return db


async def get_session(db: aiosqlite.Connection, session: str) -> SessionDict | None:
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
) -> SessionDict:
    """Create a session if it doesn't exist (idempotent). Return session dict."""
    await db.execute(
        "INSERT OR IGNORE INTO sessions (session, connector, webhook, description) VALUES (?, ?, ?, ?)",
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
) -> tuple[SessionDict, bool]:
    """Create or update a session. Returns (session_dict, created).

    If session exists: update connector, webhook, description, updated_at.
    If not: insert new row.
    """
    cur = await db.execute(
        "INSERT OR IGNORE INTO sessions (session, connector, webhook, description) VALUES (?, ?, ?, ?)",
        (session, connector, webhook, description),
    )
    created = cur.rowcount == 1
    if not created:
        await db.execute(
            "UPDATE sessions SET connector = ?, webhook = ?, description = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE session = ?",
            (connector, webhook, description, session),
        )
    await db.commit()
    return (await get_session(db, session)), created  # type: ignore[return-value]


async def save_message(
    db: aiosqlite.Connection,
    session: str,
    user: str | None,
    role: str,
    content: str,
    trusted: bool = True,
    processed: bool = False,
    source: str = "user",
) -> int:
    """Insert a message row. Bumps session updated_at. Returns message id."""
    cur = await db.execute(
        "INSERT INTO messages (session, user, role, content, trusted, processed, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session, user, role, content, trusted, processed, source),
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
    await _update_field(db, "messages", "processed", 1, msg_id)


async def mark_messages_processed(db: aiosqlite.Connection, msg_ids: list[int]) -> None:
    """Batch-mark messages as processed. No-op if list is empty."""
    if not msg_ids:
        return
    placeholders = ",".join("?" for _ in msg_ids)
    await db.execute(
        f"UPDATE messages SET processed = 1 WHERE id IN ({placeholders})", msg_ids
    )
    await db.commit()


async def get_sessions_for_user(db: aiosqlite.Connection, username: str) -> list[SessionDict]:
    """Return sessions where user has sent messages."""
    cur = await db.execute(
        "SELECT DISTINCT s.* "
        "FROM sessions s JOIN messages m ON s.session = m.session "
        "WHERE m.user = ? ORDER BY s.updated_at DESC",
        (username,),
    )
    return await _rows_to_dicts(cur)


async def session_owned_by(db: aiosqlite.Connection, session: str, username: str) -> bool:
    """Return True if *username* has posted at least one message in *session*."""
    cur = await db.execute(
        "SELECT 1 FROM messages WHERE session = ? AND user = ? LIMIT 1",
        (session, username),
    )
    return await cur.fetchone() is not None


async def get_all_sessions(db: aiosqlite.Connection) -> list[SessionDict]:
    """Return all sessions."""
    cur = await db.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    )
    return await _rows_to_dicts(cur)


async def get_tasks_for_session(
    db: aiosqlite.Connection, session: str, after: int = 0
) -> list[TaskDict]:
    """Return tasks for a session, optionally after a given id."""
    cur = await db.execute(
        "SELECT * FROM tasks WHERE session = ? AND id > ? ORDER BY id",
        (session, after),
    )
    return await _rows_to_dicts(cur)


async def get_plan_for_session(db: aiosqlite.Connection, session: str) -> PlanDict | None:
    """Return the latest plan for a session, or None."""
    cur = await db.execute(
        "SELECT * FROM plans WHERE session = ? ORDER BY id DESC LIMIT 1",
        (session,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def session_has_install_proposal(db: aiosqlite.Connection, session: str) -> bool:
    """Check if the most recent completed plan proposed a tool installation.

    Returns True when the last done/failed plan has ``install_proposal`` set.
    The flag is set server-side by ``run_planner`` when validation detected
    uninstalled-tool errors and the planner responded with a msg-only plan.
    No keyword heuristics — purely structural detection.
    """
    cur = await db.execute(
        "SELECT install_proposal FROM plans "
        "WHERE session = ? AND status IN ('done', 'failed') "
        "ORDER BY id DESC LIMIT 1",
        (session,),
    )
    row = await cur.fetchone()
    return bool(row and row["install_proposal"])


async def _get_messages_filtered(
    db: aiosqlite.Connection,
    *,
    session: str | None = None,
    trusted: int | None = None,
    processed: int | None = None,
    order: str = "ASC",
    limit: int | None = None,
    reverse: bool = False,
) -> list[dict]:
    """Parametric message query helper.

    Builds a WHERE clause from the non-None filters, applies ORDER BY id
    *order* and optional LIMIT.  When *reverse* is True the returned rows
    are reversed after fetch (used by get_recent_messages to fetch DESC
    then return oldest-first).
    """
    clauses: list[str] = []
    params: list = []
    if session is not None:
        clauses.append("session = ?")
        params.append(session)
    if trusted is not None:
        clauses.append("trusted = ?")
        params.append(trusted)
    if processed is not None:
        clauses.append("processed = ?")
        params.append(processed)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM messages{where} ORDER BY id {order}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = await db.execute(sql, params)
    rows = await _rows_to_dicts(cur)
    if reverse:
        rows.reverse()
    return rows


async def get_recent_messages(
    db: aiosqlite.Connection, session: str, limit: int = 5
) -> list[MessageDict]:
    """Return the most recent messages for a session (trusted only), oldest first."""
    return await _get_messages_filtered(
        db, session=session, trusted=1, order="DESC", limit=limit, reverse=True,
    )


def _fact_session_filter(
    is_admin: bool, session: str | None, *, prefix: str = "",
    username: str | None = None,
) -> tuple[str, list]:
    """Return (sql_fragment, params) for session-scoped fact queries.

    Admin users or ``session=None`` get no filter (empty string, no params).

    3-level scoping:
    - Global: project_id IS NULL AND category NOT IN ('user')
    - Project-scoped: project_id in user's projects (via project_members)
    - Session-scoped: category='user' AND session = current session

    *prefix* is prepended to column names (e.g. ``"f."``).
    *username* enables project-scoped filtering when provided.
    """
    if is_admin or session is None:
        return ("", [])
    p = prefix
    if username:
        return (
            f" AND ("
            f"({p}project_id IS NULL AND {p}category != 'user')"
            f" OR ({p}project_id IS NOT NULL AND {p}project_id IN"
            f" (SELECT project_id FROM project_members WHERE username = ?))"
            f" OR ({p}category = 'user' AND {p}session = ?)"
            f")",
            [username, session],
        )
    # Fallback: no username provided — use session-only filter
    return (
        f" AND ({p}category != 'user' OR {p}session = ?)",
        [session],
    )


async def get_facts(
    db: aiosqlite.Connection,
    *,
    session: str | None = None,
    is_admin: bool = False,
    limit: int | None = None,
    username: str | None = None,
) -> list[FactDict]:
    """Return facts filtered by session scope.

    - project / tool / general facts are always global and returned unconditionally.
    - user-category facts are visible only in the session where they were created.
    - Admin users bypass all filtering and receive every fact.
    - limit caps the number of rows returned (None = no cap, uses LIMIT -1 internally).
    - username enables 3-level project scoping.
    """
    limit_val = limit if limit is not None else -1
    filt, filt_params = _fact_session_filter(is_admin, session, username=username)
    if filt:
        cur = await db.execute(
            f"SELECT * FROM facts WHERE 1=1{filt} ORDER BY id LIMIT ?",
            filt_params + [limit_val],
        )
    else:
        cur = await db.execute("SELECT * FROM facts ORDER BY id LIMIT ?", (limit_val,))
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
    username: str | None = None,
) -> list[dict]:
    """Return up to *limit* facts most relevant to *query* (FTS5 BM25 ranking).

    Session scoping: user-category facts are filtered to the current session
    unless *is_admin* is ``True``.  *username* enables project scoping.

    Falls back to :func:`get_facts` when:
    - FTS5 is not compiled into the SQLite build
    - *query* contains no searchable tokens
    - The FTS search returns no results (ensures context is always present)
    """
    fts_q = _fts5_query(query)
    if not fts_q:
        return await get_facts(db, session=session, is_admin=is_admin, username=username)

    try:
        filt, filt_params = _fact_session_filter(is_admin, session, prefix="f.", username=username)
        cur = await db.execute(
            "SELECT f.* FROM facts f "
            "JOIN kiso_facts_fts fts ON fts.rowid = f.id "
            f"WHERE kiso_facts_fts MATCH ?{filt} "
            "ORDER BY rank LIMIT ?",
            [fts_q] + filt_params + [limit],
        )
        results = await _rows_to_dicts(cur)
    except Exception as exc:
        log.debug("FTS5 search failed, falling back to full scan: %s", exc, exc_info=True)
        return await get_facts(db, session=session, is_admin=is_admin, username=username)

    # If FTS found no matches, fall back to the full filtered set so the planner
    # always has some knowledge context (avoids silent empty-facts scenario).
    if not results:
        return await get_facts(db, session=session, is_admin=is_admin, username=username)
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
    duration_ms: int | None = None,
) -> None:
    """Update task status, output, stderr, duration_ms, and updated_at."""
    await _update_fields(db, "tasks", {
        "status": status, "output": output, "stderr": stderr,
        "duration_ms": duration_ms,
    }, task_id)


async def update_task_review(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: str,
    reason: str | None = None,
    learning: str | None = None,
) -> None:
    """Persist review verdict on a task row."""
    await _update_fields(db, "tasks", {
        "review_verdict": verdict, "review_reason": reason,
        "review_learning": learning,
    }, task_id)


async def update_task_command(
    db: aiosqlite.Connection, task_id: int, command: str
) -> None:
    """Set the translated shell command on a task."""
    await _update_field(db, "tasks", "command", command, task_id, update_timestamp=True)


_KEEP_LLM_CALLS = object()  # sentinel: don't touch the llm_calls column


def _serialize_llm_calls(
    llm_calls: list[dict] | None | object,
) -> tuple[bool, str | None]:
    """Resolve the *llm_calls* sentinel.

    Returns ``(should_update, json_value)``.  When *llm_calls* is the
    ``_KEEP_LLM_CALLS`` sentinel, ``should_update`` is ``False`` and the
    caller should omit the column from the UPDATE.  Otherwise the value
    is serialised to a JSON string (or ``None`` for an empty/None list).
    """
    if llm_calls is _KEEP_LLM_CALLS:
        return False, None
    return True, json.dumps(llm_calls) if llm_calls else None


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
    update, calls_json = _serialize_llm_calls(llm_calls)
    if update:
        await db.execute(
            "UPDATE tasks SET input_tokens = ?, output_tokens = ?, llm_calls = ? WHERE id = ?",
            (input_tokens, output_tokens, calls_json, task_id),
        )
    else:
        await db.execute(
            "UPDATE tasks SET input_tokens = ?, output_tokens = ? WHERE id = ?",
            (input_tokens, output_tokens, task_id),
        )
    await db.commit()


async def update_task_substatus(
    db: aiosqlite.Connection, task_id: int, substatus: str
) -> None:
    """Update only the substatus text (lightweight, no output/status change)."""
    await _update_field(db, "tasks", "substatus", substatus, task_id, update_timestamp=True)


async def update_task_retry_count(
    db: aiosqlite.Connection, task_id: int, retry_count: int
) -> None:
    """Update the retry_count on a task."""
    await _update_field(db, "tasks", "retry_count", retry_count, task_id, update_timestamp=True)


async def append_task_llm_call(
    db: aiosqlite.Connection, task_id: int, call_data: dict
) -> None:
    """Append a single LLM call entry to the task's llm_calls JSON array.

    Uses SQLite's json_insert for an atomic append — no read-modify-write
    race condition between concurrent coroutines on the same task row.
    Corrupted or NULL llm_calls are treated as an empty array.
    """
    await db.execute(
        "UPDATE tasks "
        "SET llm_calls = json_insert("
        "    CASE WHEN json_valid(llm_calls) THEN llm_calls ELSE '[]' END,"
        "    '$[#]', json(?)"
        ") WHERE id = ?",
        (json.dumps(call_data), task_id),
    )
    await db.commit()


async def update_plan_status(
    db: aiosqlite.Connection, plan_id: int, status: str
) -> None:
    """Update plan status."""
    await _update_field(db, "plans", "status", status, plan_id)


async def update_plan_goal(
    db: aiosqlite.Connection, plan_id: int, goal: str
) -> None:
    """Update plan goal."""
    await _update_field(db, "plans", "goal", goal, plan_id)


async def update_plan_install_proposal(
    db: aiosqlite.Connection, plan_id: int, value: bool = True,
) -> None:
    """Mark a plan as an install proposal."""
    await _update_field(db, "plans", "install_proposal", int(value), plan_id)


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
    update, calls_json = _serialize_llm_calls(llm_calls)
    if update:
        await db.execute(
            "UPDATE plans SET total_input_tokens = ?, total_output_tokens = ?, model = ?, llm_calls = ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, model, calls_json, plan_id),
        )
    else:
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
    return await _rows_to_dicts(cur)

# Patterns that indicate secret-like content (passwords, tokens, hex keys).
# Matched case-insensitively; learnings matching any pattern are rejected.
_SENSITIVE_PATTERN = re.compile(
    r"\b(password|passwd|token)\b"
    r"|[0-9a-fA-F]{32,}",
    re.IGNORECASE,
)


_DEDUP_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "has", "have", "had",
    "on", "in", "at", "for", "with", "of", "to", "and", "or", "but",
    "it", "its", "this", "that", "be", "been", "being",
})


def _word_overlap_ratio(a: str, b: str) -> float:
    """Return the Jaccard similarity of word sets from *a* and *b*.

    Strips stopwords and trailing punctuation before computing overlap.
    """
    wa = {w.strip(".,;:!?\"'()") for w in a.lower().split()} - _DEDUP_STOPWORDS
    wb = {w.strip(".,;:!?\"'()") for w in b.lower().split()} - _DEDUP_STOPWORDS
    wa.discard("")
    wb.discard("")
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


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
    - *content* is a near-duplicate of an existing pending learning in the same
      session (word overlap ≥ 55%)

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
    # Dedup against pending learnings in the same session
    cur = await db.execute(
        "SELECT id, content FROM learnings WHERE session = ? AND status = 'pending'",
        (session,),
    )
    for row in await cur.fetchall():
        if _word_overlap_ratio(content, row[1]) >= 0.55:
            log.debug("Learning deduped against id=%d", row[0])
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
    await _update_field(db, "learnings", "status", status, learning_id)


async def save_fact(
    db: aiosqlite.Connection,
    content: str,
    source: str,
    session: str | None = None,
    category: str = "general",
    confidence: float = 1.0,
    tags: list[str] | None = None,
    entity_id: int | None = None,
    project_id: int | None = None,
) -> int:
    """Insert a fact row and optional tags. Returns fact id.

    *project_id* scopes the fact to a project (M685/M689).
    """
    cur = await db.execute(
        "INSERT INTO facts (content, source, session, category, confidence, entity_id, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (content, source, session, category, confidence, entity_id, project_id),
    )
    fact_id: int = cur.lastrowid  # type: ignore[assignment]
    if tags:
        await db.executemany(
            "INSERT OR IGNORE INTO fact_tags (fact_id, tag) VALUES (?, ?)",
            [(fact_id, t) for t in tags],
        )
    await db.commit()
    return fact_id


async def save_facts_batch(
    db: aiosqlite.Connection,
    facts: list[dict],
) -> None:
    """Insert multiple facts in one transaction.

    Each dict must have ``content`` and ``source``; optionally ``session``,
    ``category`` (default ``"general"``), and ``confidence`` (default ``1.0``).
    """
    rows = [
        (
            f["content"],
            f["source"],
            f.get("session"),
            f.get("category", "general"),
            float(f.get("confidence", 1.0)),
        )
        for f in facts
    ]
    await db.executemany(
        "INSERT INTO facts (content, source, session, category, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


async def save_fact_tags(
    db: aiosqlite.Connection,
    fact_id: int,
    tags: list[str],
) -> None:
    """Insert tags for a fact (idempotent — duplicates ignored)."""
    if not tags:
        return
    await db.executemany(
        "INSERT OR IGNORE INTO fact_tags (fact_id, tag) VALUES (?, ?)",
        [(fact_id, t) for t in tags],
    )
    await db.commit()


async def get_all_tags(db: aiosqlite.Connection) -> list[str]:
    """Return all distinct tags from fact_tags, sorted alphabetically."""
    cur = await db.execute("SELECT DISTINCT tag FROM fact_tags ORDER BY tag")
    rows = await cur.fetchall()
    return [r[0] for r in rows]


async def search_facts_by_tags(
    db: aiosqlite.Connection,
    tags: list[str],
    session: str | None = None,
    is_admin: bool = False,
    username: str | None = None,
) -> list[dict]:
    """Return facts that have ANY of the given tags, ranked by tag overlap count.

    Non-admin users only see facts from their session or global facts.
    *username* enables project scoping.
    """
    if not tags:
        return []
    placeholders = ", ".join("?" for _ in tags)
    filt, filt_params = _fact_session_filter(is_admin, session, prefix="f.", username=username)
    query = (
        f"SELECT f.*, COUNT(ft.tag) AS tag_overlap "
        f"FROM facts f "
        f"JOIN fact_tags ft ON f.id = ft.fact_id "
        f"WHERE ft.tag IN ({placeholders}){filt} "
        f"GROUP BY f.id ORDER BY tag_overlap DESC, f.use_count DESC"
    )
    cur = await db.execute(query, list(tags) + filt_params)
    return [dict(r) for r in await cur.fetchall()]


def _normalize_entity_name(name: str) -> str:
    """Canonical entity name: lowercase, no www/http prefix, no trailing slash."""
    n = name.lower().strip()
    for prefix in ("https://", "http://", "www."):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n.rstrip("/")


async def find_or_create_entity(
    db: aiosqlite.Connection, name: str, kind: str,
) -> int:
    """Find entity by canonical name or create it. Returns entity_id."""
    canonical = _normalize_entity_name(name)
    cur = await db.execute("SELECT id, kind FROM entities WHERE name = ?", (canonical,))
    existing = await cur.fetchone()
    if existing:
        # update kind if caller provides different classification
        if existing["kind"] != kind:
            await db.execute(
                "UPDATE entities SET kind = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (kind, existing["id"]),
            )
            await db.commit()
            log.info("Entity '%s' kind updated: %s → %s", canonical, existing["kind"], kind)
        return existing["id"]
    cur = await db.execute(
        "INSERT INTO entities (name, kind) VALUES (?, ?)", (canonical, kind),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def get_all_entities(db: aiosqlite.Connection) -> list[dict]:
    """Return all entities as [{id, name, kind}, ...]."""
    cur = await db.execute("SELECT id, name, kind FROM entities ORDER BY name")
    return [dict(r) for r in await cur.fetchall()]


async def search_facts_by_entity(
    db: aiosqlite.Connection, entity_id: int,
) -> list[dict]:
    """Return all facts for a given entity, ordered by last_used desc."""
    cur = await db.execute(
        "SELECT * FROM facts WHERE entity_id = ? ORDER BY last_used DESC, id DESC",
        (entity_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def _search_facts_by_entity_tags(
    db: aiosqlite.Connection,
    *,
    entity_id: int | None,
    tags: list[str] | None,
    session_filter: str,
    session_params: list,
    fetch_limit: int,
) -> list[dict]:
    """SQL-scored fact retrieval by entity and/or tags.

    Returns rows with ``entity_score`` and ``tag_score`` columns attached.
    Called internally by :func:`search_facts_scored`.
    """
    params: list = []
    select_parts = ["f.*"]
    join_parts: list[str] = []

    # Entity score
    if entity_id is not None:
        select_parts.append("CASE WHEN f.entity_id = ? THEN 10 ELSE 0 END AS entity_score")
        params.append(entity_id)
    else:
        select_parts.append("0 AS entity_score")

    # Tag score
    if tags:
        placeholders = ", ".join("?" for _ in tags)
        join_parts.append(
            f"LEFT JOIN ("
            f"  SELECT fact_id, COUNT(*) AS tag_count"
            f"  FROM fact_tags WHERE tag IN ({placeholders})"
            f"  GROUP BY fact_id"
            f") _tc ON _tc.fact_id = f.id"
        )
        params.extend(tags)
        select_parts.append("COALESCE(_tc.tag_count, 0) * 3 AS tag_score")
    else:
        select_parts.append("0 AS tag_score")

    # WHERE: at least one signal must match
    or_conditions: list[str] = []
    if entity_id is not None:
        or_conditions.append("f.entity_id = ?")
        params.append(entity_id)
    if tags:
        or_conditions.append("_tc.tag_count > 0")

    params.extend(session_params)
    where_sql = " OR ".join(or_conditions)
    query = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM facts f "
        f"{' '.join(join_parts)} "
        f"WHERE ({where_sql}){session_filter} "
        f"ORDER BY (entity_score + tag_score) DESC, "
        f"COALESCE(f.last_used, f.created_at) DESC "
        f"LIMIT ?"
    )
    params.append(fetch_limit)
    cur = await db.execute(query, params)
    return [dict(r) for r in await cur.fetchall()]


async def _search_facts_by_keywords(
    db: aiosqlite.Connection,
    keywords: list[str],
    *,
    session_filter: str,
    session_params: list,
    fetch_limit: int,
) -> list[dict]:
    """FTS5 keyword search for facts (no entity/tag scoring).

    Returns rows with ``entity_score=0`` and ``tag_score=0`` columns.
    Called internally by :func:`search_facts_scored`.
    """
    fts_q = _fts5_query(" ".join(keywords))
    if not fts_q:
        return []
    query = (
        "SELECT f.*, 0 AS entity_score, 0 AS tag_score "
        "FROM facts f "
        "JOIN kiso_facts_fts fts ON fts.rowid = f.id "
        f"WHERE kiso_facts_fts MATCH ?{session_filter} "
        "ORDER BY rank LIMIT ?"
    )
    params: list = [fts_q] + session_params + [fetch_limit]
    try:
        cur = await db.execute(query, params)
        return [dict(r) for r in await cur.fetchall()]
    except Exception:
        return []


async def search_facts_scored(
    db: aiosqlite.Connection,
    *,
    entity_id: int | None = None,
    tags: list[str] | None = None,
    keywords: list[str] | None = None,
    session: str | None = None,
    is_admin: bool = True,
    limit: int = 50,
    username: str | None = None,
) -> list[dict]:
    """Score and rank facts by relevance.

    Score = (entity_match x 10) + (tag_overlap_count x 3) + (keyword_hit x 1).
    Returns top *limit* facts ordered by score desc, then last_used desc.

    At least one of *entity_id*, *tags*, or *keywords* must be provided.
    *username* enables project scoping.
    """
    if not entity_id and not tags and not keywords:
        return []

    session_filter, session_params = _fact_session_filter(is_admin, session, prefix="f.", username=username)
    sp = list(session_params)  # materialise once for reuse
    fetch_limit = limit * 2  # over-fetch for Python re-rank

    has_entity_or_tags = entity_id is not None or bool(tags)
    if has_entity_or_tags:
        rows = await _search_facts_by_entity_tags(
            db,
            entity_id=entity_id,
            tags=tags,
            session_filter=session_filter,
            session_params=sp,
            fetch_limit=fetch_limit,
        )
        # M962: tagless facts shouldn't become unfindable — fall back to
        # keyword FTS when entity/tag search yields nothing.
        if not rows and keywords:
            rows = await _search_facts_by_keywords(
                db,
                keywords,
                session_filter=session_filter,
                session_params=sp,
                fetch_limit=fetch_limit,
            )
    else:
        rows = await _search_facts_by_keywords(
            db,
            keywords or [],
            session_filter=session_filter,
            session_params=sp,
            fetch_limit=fetch_limit,
        )

    # --- Keyword scoring in Python ---
    kw_set = {w.lower() for w in (keywords or [])} if keywords else set()
    scored: list[tuple[int, dict]] = []
    for row in rows:
        base = row.get("entity_score", 0) + row.get("tag_score", 0)
        if kw_set:
            content_lower = row["content"].lower()
            kw_hits = sum(1 for kw in kw_set if kw in content_lower)
            base += kw_hits
        scored.append((base, row))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Clean up scoring columns from output
    results: list[dict] = []
    for _, row in scored[:limit]:
        row.pop("entity_score", None)
        row.pop("tag_score", None)
        row.pop("tag_count", None)
        results.append(row)
    return results


async def backfill_fact_entities(db: aiosqlite.Connection) -> int:
    """Backfill entity_id for facts that match known entities by content.

    Facts with entity_id=NULL are
    invisible to entity-scoped queries.  This scans NULL-entity facts and links
    them when the fact content mentions a known entity name.
    """
    entities = await get_all_entities(db)
    if not entities:
        return 0
    orphan_cur = await db.execute(
        "SELECT id, content FROM facts WHERE entity_id IS NULL",
    )
    orphans = await orphan_cur.fetchall()
    if not orphans:
        return 0
    updated = 0
    for row in orphans:
        content_lower = row["content"].lower()
        for entity in entities:
            # word-boundary match to avoid "java" matching "javascript"
            if re.search(r'\b' + re.escape(entity["name"]) + r'\b', content_lower):
                await db.execute(
                    "UPDATE facts SET entity_id = ? WHERE id = ?",
                    (entity["id"], row["id"]),
                )
                updated += 1
                break  # first matching entity wins
    if updated:
        await db.commit()
    return updated


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
    await _update_field(
        db, "sessions", "summary", summary, session,
        id_column="session", update_timestamp=True,
    )


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
    return await _get_messages_filtered(
        db, session=session, trusted=1, order="ASC", limit=limit,
    )


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
    return await _get_messages_filtered(
        db, session=session, trusted=0, order="ASC", limit=limit,
    )


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
    return await _get_messages_filtered(
        db, trusted=1, processed=0, order="ASC",
    )


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


async def get_safety_facts(db: aiosqlite.Connection) -> list[dict]:
    """Return all safety-category facts, ordered by creation time."""
    cur = await db.execute(
        "SELECT id, content FROM facts WHERE category = 'safety' "
        "ORDER BY created_at",
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_behavior_facts(db: aiosqlite.Connection) -> list[dict]:
    """M671: Return all behavior-category facts, ordered by creation time."""
    cur = await db.execute(
        "SELECT id, content FROM facts WHERE category = 'behavior' "
        "ORDER BY created_at",
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_knowledge(
    db: aiosqlite.Connection,
    *,
    category: str | None = None,
    entity: str | None = None,
    tag: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """M672: List facts with optional filters. Returns dicts with id, content,
    category, entity_name, entity_kind, tags, confidence, created_at.
    """
    if search:
        # FTS5 search path
        fts_q = _fts5_query(search)
        if fts_q:
            try:
                cur = await db.execute(
                    "SELECT f.id, f.content, f.category, f.confidence, f.created_at, "
                    "e.name AS entity_name, e.kind AS entity_kind "
                    "FROM facts f "
                    "LEFT JOIN entities e ON f.entity_id = e.id "
                    "JOIN kiso_facts_fts fts ON fts.rowid = f.id "
                    "WHERE kiso_facts_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (fts_q, limit),
                )
                rows = [dict(r) for r in await cur.fetchall()]
            except Exception:
                rows = []
            if rows:
                return await _attach_tags(db, rows)

    # Filtered query
    clauses: list[str] = []
    params: list = []
    if category:
        clauses.append("f.category = ?")
        params.append(category)
    if entity:
        clauses.append("LOWER(e.name) = LOWER(?)")
        params.append(entity)
    if tag:
        clauses.append("f.id IN (SELECT fact_id FROM fact_tags WHERE tag = ?)")
        params.append(tag.lower())

    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    cur = await db.execute(
        "SELECT f.id, f.content, f.category, f.confidence, f.created_at, "
        "e.name AS entity_name, e.kind AS entity_kind "
        f"FROM facts f LEFT JOIN entities e ON f.entity_id = e.id "
        f"WHERE 1=1{where} ORDER BY f.id DESC LIMIT ?",
        params + [limit],
    )
    rows = [dict(r) for r in await cur.fetchall()]
    return await _attach_tags(db, rows)


async def _attach_tags(db: aiosqlite.Connection, rows: list[dict]) -> list[dict]:
    """Attach tags list to each fact row."""
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    cur = await db.execute(
        f"SELECT fact_id, tag FROM fact_tags WHERE fact_id IN ({placeholders})",
        ids,
    )
    tag_rows = await cur.fetchall()
    tag_map: dict[int, list[str]] = {}
    for tr in tag_rows:
        tag_map.setdefault(tr["fact_id"], []).append(tr["tag"])
    for r in rows:
        r["tags"] = tag_map.get(r["id"], [])
    return rows


async def decay_facts(
    db: aiosqlite.Connection,
    decay_days: int = 7,
    decay_rate: float = 0.1,
) -> int:
    """Reduce confidence of facts not used in decay_days. Returns rows affected.

    Safety facts are excluded — they never decay.
    """
    cur = await db.execute(
        "UPDATE facts SET confidence = MAX(0.0, confidence - ?) "
        "WHERE COALESCE(last_used, created_at) < datetime('now', ?) "
        "AND category != 'safety'",
        (decay_rate, f"-{decay_days} days"),
    )
    await db.commit()
    return cur.rowcount


async def archive_low_confidence_facts(
    db: aiosqlite.Connection, threshold: float = 0.3
) -> int:
    """Copy facts with confidence < threshold to facts_archive, then delete. Returns rows archived.

    Safety facts are excluded — they are never archived or deleted.
    """
    cur = await db.execute(
        "INSERT INTO facts_archive (original_id, content, source, session, "
        "category, confidence, last_used, use_count, created_at) "
        "SELECT id, content, source, session, category, confidence, "
        "last_used, use_count, created_at FROM facts "
        "WHERE confidence < ? AND category != 'safety'",
        (threshold,),
    )
    archived = cur.rowcount
    if archived:
        await db.execute(
            "DELETE FROM facts WHERE confidence < ? AND category != 'safety'",
            (threshold,),
        )
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
    parallel_group: int | None = None,
) -> int:
    """Insert a task row. Returns task id."""
    cur = await db.execute(
        "INSERT INTO tasks (plan_id, session, type, detail, skill, args, expect, parallel_group) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (plan_id, session, type, detail, skill, args, expect, parallel_group),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


# --- Cron job store functions ---


async def create_cron_job(
    db: aiosqlite.Connection,
    session: str,
    schedule: str,
    prompt: str,
    created_by: str,
    next_run: str,
) -> int:
    """Insert a cron job. Returns job id."""
    cur = await db.execute(
        "INSERT INTO cron_jobs (session, schedule, prompt, created_by, next_run) "
        "VALUES (?, ?, ?, ?, ?)",
        (session, schedule, prompt, created_by, next_run),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def list_cron_jobs(
    db: aiosqlite.Connection, session: str | None = None,
) -> list[dict]:
    """List cron jobs, optionally filtered by session."""
    if session:
        cur = await db.execute(
            "SELECT * FROM cron_jobs WHERE session = ? ORDER BY id", (session,),
        )
    else:
        cur = await db.execute("SELECT * FROM cron_jobs ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


async def delete_cron_job(db: aiosqlite.Connection, job_id: int) -> bool:
    """Delete a cron job. Returns True if deleted."""
    cur = await db.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cur.rowcount > 0


async def update_cron_enabled(db: aiosqlite.Connection, job_id: int, enabled: bool) -> bool:
    """Enable or disable a cron job. Returns True if updated."""
    cur = await db.execute(
        "UPDATE cron_jobs SET enabled = ? WHERE id = ?", (int(enabled), job_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_due_cron_jobs(db: aiosqlite.Connection, now_iso: str) -> list[dict]:
    """Return enabled cron jobs whose next_run <= now.

    Uses SQLite datetime() to normalize both sides, making the comparison
    robust against timezone offset inconsistencies.
    """
    cur = await db.execute(
        "SELECT * FROM cron_jobs WHERE enabled = 1 AND datetime(next_run) <= datetime(?) "
        "ORDER BY next_run",
        (now_iso,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def update_cron_last_run(
    db: aiosqlite.Connection, job_id: int, last_run: str, next_run: str,
) -> None:
    """Update last_run and next_run after a cron job fires."""
    await db.execute(
        "UPDATE cron_jobs SET last_run = ?, next_run = ? WHERE id = ?",
        (last_run, next_run, job_id),
    )
    await db.commit()


# --- Project store functions ---


async def create_project(
    db: aiosqlite.Connection, name: str, created_by: str, description: str = "",
) -> int:
    """Create a project. Returns project id."""
    cur = await db.execute(
        "INSERT INTO projects (name, description, created_by) VALUES (?, ?, ?)",
        (name, description, created_by),
    )
    project_id: int = cur.lastrowid  # type: ignore[assignment]
    # Creator is automatically a member
    await db.execute(
        "INSERT INTO project_members (project_id, username, role) VALUES (?, ?, 'member')",
        (project_id, created_by),
    )
    await db.commit()
    return project_id


async def get_project(db: aiosqlite.Connection, name: str) -> dict | None:
    """Get a project by name."""
    cur = await db.execute("SELECT * FROM projects WHERE name = ?", (name,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_projects(
    db: aiosqlite.Connection, username: str | None = None,
) -> list[dict]:
    """List projects. If username provided, only projects where user is member/viewer."""
    if username:
        cur = await db.execute(
            "SELECT p.* FROM projects p "
            "JOIN project_members pm ON p.id = pm.project_id "
            "WHERE pm.username = ? ORDER BY p.id",
            (username,),
        )
    else:
        cur = await db.execute("SELECT * FROM projects ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


async def delete_project(db: aiosqlite.Connection, project_id: int) -> bool:
    """Delete a project (cascades to members)."""
    cur = await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    await db.commit()
    return cur.rowcount > 0


async def add_project_member(
    db: aiosqlite.Connection, project_id: int, username: str, role: str = "member",
) -> None:
    """Add a member to a project (or update role if already member)."""
    await db.execute(
        "INSERT OR REPLACE INTO project_members (project_id, username, role) "
        "VALUES (?, ?, ?)",
        (project_id, username, role),
    )
    await db.commit()


async def remove_project_member(
    db: aiosqlite.Connection, project_id: int, username: str,
) -> bool:
    """Remove a member from a project. Returns True if removed."""
    cur = await db.execute(
        "DELETE FROM project_members WHERE project_id = ? AND username = ?",
        (project_id, username),
    )
    await db.commit()
    return cur.rowcount > 0


async def list_project_members(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    """List members of a project."""
    cur = await db.execute(
        "SELECT username, role FROM project_members WHERE project_id = ? ORDER BY username",
        (project_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def bind_session_to_project(
    db: aiosqlite.Connection, session: str, project_id: int,
) -> None:
    """M684: Associate a session with a project."""
    await db.execute(
        "UPDATE sessions SET project_id = ? WHERE session = ?",
        (project_id, session),
    )
    await db.commit()


async def unbind_session_from_project(db: aiosqlite.Connection, session: str) -> None:
    """M684: Remove project association from a session."""
    await db.execute(
        "UPDATE sessions SET project_id = NULL WHERE session = ?", (session,),
    )
    await db.commit()


async def get_session_project_id(db: aiosqlite.Connection, session: str) -> int | None:
    """M684: Return the project_id for a session, or None."""
    cur = await db.execute(
        "SELECT project_id FROM sessions WHERE session = ?", (session,),
    )
    row = await cur.fetchone()
    return row["project_id"] if row else None


async def get_user_project_role(
    db: aiosqlite.Connection, project_id: int, username: str,
) -> str | None:
    """Get a user's role in a project (member/viewer/None)."""
    cur = await db.execute(
        "SELECT role FROM project_members WHERE project_id = ? AND username = ?",
        (project_id, username),
    )
    row = await cur.fetchone()
    return row["role"] if row else None


# ---------------------------------------------------------------------------
# Key-Value helpers (used for dream timestamp, etc.)
# ---------------------------------------------------------------------------


async def get_kv(db: aiosqlite.Connection, key: str) -> str | None:
    """Return value for *key* from the kv table, or None."""
    cur = await db.execute("SELECT value FROM kv WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else None


async def set_kv(db: aiosqlite.Connection, key: str, value: str) -> None:
    """Insert or replace a key-value pair."""
    await db.execute(
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value),
    )
    await db.commit()


async def update_fact_content(db: aiosqlite.Connection, fact_id: int, content: str) -> None:
    """Update a fact's content text in place."""
    await db.execute("UPDATE facts SET content = ? WHERE id = ?", (content, fact_id))
    await db.commit()


