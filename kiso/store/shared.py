"""Shared store schema, types, and low-level SQL helpers."""

from __future__ import annotations

import json
import logging
import re
from typing import TypedDict

import aiosqlite

log = logging.getLogger(__name__)


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
    wrapper: str | None
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
    project_id  INTEGER REFERENCES projects(id),
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
    source    TEXT DEFAULT 'user',
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
    wrapper      TEXT,
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
    parallel_group INTEGER,
    review_learning_tags TEXT,
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
    project_id INTEGER REFERENCES projects(id),
    entity_id  INTEGER REFERENCES entities(id),
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


async def _row_to_dict(cur: aiosqlite.Cursor) -> dict | None:
    """Fetch one row from a cursor and return it as a dict."""
    row = await cur.fetchone()
    return dict(row) if row else None


def _json_text_or_none(value: object, *, sort_keys: bool = False) -> str | None:
    """Serialize a JSON payload or return ``None`` for empty values."""
    if value in (None, [], {}):
        return None
    return json.dumps(value, sort_keys=sort_keys)


def _serialize_task_args(args: str | dict | None) -> str | None:
    """Normalize task args at the DB boundary."""
    if isinstance(args, dict):
        return _json_text_or_none(args, sort_keys=True)
    return args


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
    """Generic single-field UPDATE helper."""
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
    """Generic multi-field UPDATE helper."""
    set_clauses = [f"{k} = ?" for k in fields]
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {id_column} = ?"
    await db.execute(sql, (*fields.values(), row_id))
    await db.commit()


_KEEP_LLM_CALLS = object()


def _serialize_llm_calls(
    llm_calls: list[dict] | None | object,
) -> tuple[bool, str | None]:
    """Resolve the *llm_calls* sentinel."""
    if llm_calls is _KEEP_LLM_CALLS:
        return False, None
    return True, _json_text_or_none(llm_calls)


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
    """Return the Jaccard similarity of word sets from *a* and *b*."""
    wa = {w.strip(".,;:!?\"'()") for w in a.lower().split()} - _DEDUP_STOPWORDS
    wb = {w.strip(".,;:!?\"'()") for w in b.lower().split()} - _DEDUP_STOPWORDS
    wa.discard("")
    wb.discard("")
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
