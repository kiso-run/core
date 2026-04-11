"""Session, message, and lightweight retrieval helpers."""

from __future__ import annotations

from typing import cast

import aiosqlite

from .shared import (
    MessageDict,
    PlanDict,
    SessionDict,
    TaskDict,
    _row_to_dict,
    _rows_to_dicts,
    _update_field,
)


async def get_session(db: aiosqlite.Connection, session: str) -> SessionDict | None:
    cur = await db.execute("SELECT * FROM sessions WHERE session = ?", (session,))
    row = await _row_to_dict(cur)
    return cast(SessionDict | None, row)


async def create_session(
    db: aiosqlite.Connection,
    session: str,
    connector: str | None = None,
    webhook: str | None = None,
    description: str | None = None,
) -> SessionDict:
    await db.execute(
        "INSERT OR IGNORE INTO sessions (session, connector, webhook, description) VALUES (?, ?, ?, ?)",
        (session, connector, webhook, description),
    )
    await db.commit()
    return cast(SessionDict, await get_session(db, session))


async def upsert_session(
    db: aiosqlite.Connection,
    session: str,
    connector: str | None = None,
    webhook: str | None = None,
    description: str | None = None,
) -> tuple[SessionDict, bool]:
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
    return cast(SessionDict, await get_session(db, session)), created


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
    return cast(int, msg_id)


async def mark_message_processed(db: aiosqlite.Connection, msg_id: int) -> None:
    await _update_field(db, "messages", "processed", 1, msg_id)


async def mark_messages_processed(db: aiosqlite.Connection, msg_ids: list[int]) -> None:
    if not msg_ids:
        return
    placeholders = ",".join("?" for _ in msg_ids)
    await db.execute(
        f"UPDATE messages SET processed = 1 WHERE id IN ({placeholders})", msg_ids
    )
    await db.commit()


async def get_sessions_for_user(db: aiosqlite.Connection, username: str) -> list[SessionDict]:
    cur = await db.execute(
        "SELECT DISTINCT s.* "
        "FROM sessions s JOIN messages m ON s.session = m.session "
        "WHERE m.user = ? ORDER BY s.updated_at DESC",
        (username,),
    )
    return cast(list[SessionDict], await _rows_to_dicts(cur))


async def session_owned_by(db: aiosqlite.Connection, session: str, username: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM messages WHERE session = ? AND user = ? LIMIT 1",
        (session, username),
    )
    return await cur.fetchone() is not None


async def get_all_sessions(db: aiosqlite.Connection) -> list[SessionDict]:
    cur = await db.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
    return cast(list[SessionDict], await _rows_to_dicts(cur))


async def get_tasks_for_session(
    db: aiosqlite.Connection, session: str, after: int = 0,
) -> list[TaskDict]:
    cur = await db.execute(
        "SELECT * FROM tasks WHERE session = ? AND id > ? ORDER BY id",
        (session, after),
    )
    return cast(list[TaskDict], await _rows_to_dicts(cur))


async def get_plan_for_session(db: aiosqlite.Connection, session: str) -> PlanDict | None:
    cur = await db.execute(
        "SELECT * FROM plans WHERE session = ? ORDER BY id DESC LIMIT 1",
        (session,),
    )
    row = await cur.fetchone()
    return cast(PlanDict | None, dict(row) if row else None)


async def session_has_install_proposal(db: aiosqlite.Connection, session: str) -> bool:
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
) -> list[MessageDict]:
    """Parametric message query helper."""
    clauses: list[str] = []
    params: list[object] = []
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
    rows = cast(list[MessageDict], await _rows_to_dicts(cur))
    if reverse:
        rows.reverse()
    return rows


async def get_recent_messages(
    db: aiosqlite.Connection, session: str, limit: int = 20,
) -> list[MessageDict]:
    return await _get_messages_filtered(
        db, session=session, trusted=1, order="DESC", limit=limit, reverse=True,
    )


def _fact_session_filter(
    is_admin: bool,
    session: str | None,
    *,
    prefix: str = "",
    username: str | None = None,
    project_id: int | None = None,
) -> tuple[str, list]:
    """Return (sql_fragment, params) for session-scoped fact queries.

    ``is_admin`` bypasses *session* scoping (user-category visibility
    across sessions) but does NOT bypass *project* scoping.  When a
    ``project_id`` or ``username`` is supplied the project filter is
    always applied, even for admins.

    Project visibility rules:
    - With ``username``: see globals + facts of any project the user is a
      member of + (non-admin: own-session user-category facts).
    - With ``project_id`` (no username, e.g. messenger queries): see globals
      + facts of that one project + (non-admin: own-session user-category).
    - With neither: admin → no filter; non-admin → ONLY globals +
      own-session user-category facts.  Project-scoped facts MUST NOT
      leak through the non-admin path.
    """
    p = prefix

    # --- project-scoped paths (always applied, even for admin) ---
    if username:
        if is_admin or session is None:
            # Admin + username: project membership filter, no session restriction
            return (
                f" AND ("
                f"{p}project_id IS NULL"
                f" OR ({p}project_id IS NOT NULL AND {p}project_id IN"
                f" (SELECT project_id FROM project_members WHERE username = ?))"
                f")",
                [username],
            )
        return (
            f" AND ("
            f"({p}project_id IS NULL AND {p}category != 'user')"
            f" OR ({p}project_id IS NOT NULL AND {p}project_id IN"
            f" (SELECT project_id FROM project_members WHERE username = ?))"
            f" OR ({p}category = 'user' AND {p}session = ?)"
            f")",
            [username, session],
        )
    if project_id is not None:
        if is_admin or session is None:
            # Admin + project_id: project filter, no session restriction
            return (
                f" AND ({p}project_id IS NULL OR {p}project_id = ?)",
                [project_id],
            )
        return (
            f" AND ("
            f"({p}project_id IS NULL AND {p}category != 'user')"
            f" OR ({p}project_id = ?)"
            f" OR ({p}category = 'user' AND {p}session = ?)"
            f")",
            [project_id, session],
        )

    # --- no project context ---
    if session is None:
        # System-level queries (consolidator, dedup) — no session context,
        # no project filter.  These callers need cross-project visibility.
        return ("", [])
    if is_admin:
        # Admin with an active session but no project binding: bypass
        # session scoping but still exclude project-scoped facts.
        return (f" AND ({p}project_id IS NULL)", [])
    return (
        f" AND ({p}project_id IS NULL)"
        f" AND ({p}category != 'user' OR {p}session = ?)",
        [session],
    )


async def get_facts(
    db: aiosqlite.Connection,
    session: str | None = None,
    is_admin: bool = False,
    limit: int | None = None,
    username: str | None = None,
    project_id: int | None = None,
) -> list[dict]:
    """Return facts filtered by session scope."""
    limit_val = limit if limit is not None else -1
    filt, params = _fact_session_filter(
        is_admin, session, username=username, project_id=project_id,
    )
    if filt:
        cur = await db.execute(
            f"SELECT * FROM facts WHERE 1=1{filt} ORDER BY id LIMIT ?",
            params + [limit_val],
        )
    else:
        cur = await db.execute("SELECT * FROM facts ORDER BY id LIMIT ?", (limit_val,))
    return await _rows_to_dicts(cur)


async def get_pending_items(db: aiosqlite.Connection, session: str) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM pending WHERE status = 'open' "
        "AND (scope = 'global' OR scope = ?) ORDER BY id",
        (session,),
    )
    return await _rows_to_dicts(cur)


async def save_pending_item(
    db: aiosqlite.Connection,
    content: str,
    scope: str,
    source: str,
) -> int:
    cur = await db.execute(
        "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
        (content, scope, source),
    )
    await db.commit()
    return cast(int, cur.lastrowid)


async def update_summary(db: aiosqlite.Connection, session: str, summary: str) -> None:
    await _update_field(
        db, "sessions", "summary", summary, session,
        id_column="session", update_timestamp=True,
    )


async def count_messages(db: aiosqlite.Connection, session: str) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM messages WHERE session = ? AND trusted = 1",
        (session,),
    )
    row = await cur.fetchone()
    return cast(int, row[0])


async def get_oldest_messages(
    db: aiosqlite.Connection, session: str, limit: int,
) -> list[dict]:
    return await _get_messages_filtered(
        db, session=session, trusted=1, order="ASC", limit=limit,
    )


async def get_untrusted_messages(
    db: aiosqlite.Connection, session: str, limit: int = 20,
) -> list[dict]:
    return await _get_messages_filtered(
        db, session=session, trusted=0, order="ASC", limit=limit,
    )


async def recover_stale_running(db: aiosqlite.Connection) -> tuple[int, int]:
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
    return await _get_messages_filtered(db, trusted=1, processed=0, order="ASC")
