"""Cron, project, and kv store helpers."""

from __future__ import annotations

from typing import cast

import aiosqlite

from .shared import _row_to_dict, _rows_to_dicts


async def create_cron_job(
    db: aiosqlite.Connection,
    session: str,
    schedule: str,
    prompt: str,
    created_by: str,
    next_run: str,
) -> int:
    cur = await db.execute(
        "INSERT INTO cron_jobs (session, schedule, prompt, created_by, next_run) "
        "VALUES (?, ?, ?, ?, ?)",
        (session, schedule, prompt, created_by, next_run),
    )
    await db.commit()
    return cast(int, cur.lastrowid)


async def list_cron_jobs(
    db: aiosqlite.Connection, session: str | None = None,
) -> list[dict]:
    if session:
        cur = await db.execute(
            "SELECT * FROM cron_jobs WHERE session = ? ORDER BY id", (session,),
        )
    else:
        cur = await db.execute("SELECT * FROM cron_jobs ORDER BY id")
    return await _rows_to_dicts(cur)


async def delete_cron_job(db: aiosqlite.Connection, job_id: int) -> bool:
    cur = await db.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cur.rowcount > 0


async def update_cron_enabled(db: aiosqlite.Connection, job_id: int, enabled: bool) -> bool:
    cur = await db.execute(
        "UPDATE cron_jobs SET enabled = ? WHERE id = ?", (int(enabled), job_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_due_cron_jobs(db: aiosqlite.Connection, now_iso: str) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM cron_jobs WHERE enabled = 1 AND datetime(next_run) <= datetime(?) "
        "ORDER BY next_run",
        (now_iso,),
    )
    return await _rows_to_dicts(cur)


async def update_cron_last_run(
    db: aiosqlite.Connection, job_id: int, last_run: str, next_run: str,
) -> None:
    await db.execute(
        "UPDATE cron_jobs SET last_run = ?, next_run = ? WHERE id = ?",
        (last_run, next_run, job_id),
    )
    await db.commit()


async def create_project(
    db: aiosqlite.Connection, name: str, created_by: str, description: str = "",
) -> int:
    cur = await db.execute(
        "INSERT INTO projects (name, description, created_by) VALUES (?, ?, ?)",
        (name, description, created_by),
    )
    project_id = cast(int, cur.lastrowid)
    await db.execute(
        "INSERT INTO project_members (project_id, username, role) VALUES (?, ?, 'member')",
        (project_id, created_by),
    )
    await db.commit()
    return project_id


async def get_project(db: aiosqlite.Connection, name: str) -> dict | None:
    cur = await db.execute("SELECT * FROM projects WHERE name = ?", (name,))
    return await _row_to_dict(cur)


async def list_projects(
    db: aiosqlite.Connection, username: str | None = None,
) -> list[dict]:
    if username:
        cur = await db.execute(
            "SELECT p.* FROM projects p "
            "JOIN project_members pm ON p.id = pm.project_id "
            "WHERE pm.username = ? ORDER BY p.id",
            (username,),
        )
    else:
        cur = await db.execute("SELECT * FROM projects ORDER BY id")
    return await _rows_to_dicts(cur)


async def delete_project(db: aiosqlite.Connection, project_id: int) -> bool:
    cur = await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    await db.commit()
    return cur.rowcount > 0


async def add_project_member(
    db: aiosqlite.Connection, project_id: int, username: str, role: str = "member",
) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO project_members (project_id, username, role) VALUES (?, ?, ?)",
        (project_id, username, role),
    )
    await db.commit()


async def remove_project_member(
    db: aiosqlite.Connection, project_id: int, username: str,
) -> bool:
    cur = await db.execute(
        "DELETE FROM project_members WHERE project_id = ? AND username = ?",
        (project_id, username),
    )
    await db.commit()
    return cur.rowcount > 0


async def list_project_members(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT username, role FROM project_members WHERE project_id = ? ORDER BY username",
        (project_id,),
    )
    return await _rows_to_dicts(cur)


async def bind_session_to_project(
    db: aiosqlite.Connection, session: str, project_id: int,
) -> None:
    await db.execute(
        "UPDATE sessions SET project_id = ? WHERE session = ?",
        (project_id, session),
    )
    await db.commit()


async def unbind_session_from_project(db: aiosqlite.Connection, session: str) -> None:
    await db.execute(
        "UPDATE sessions SET project_id = NULL WHERE session = ?", (session,),
    )
    await db.commit()


async def get_session_project_id(db: aiosqlite.Connection, session: str) -> int | None:
    cur = await db.execute(
        "SELECT project_id FROM sessions WHERE session = ?", (session,),
    )
    row = await cur.fetchone()
    return cast(int | None, row["project_id"] if row else None)


async def get_user_project_role(
    db: aiosqlite.Connection, project_id: int, username: str,
) -> str | None:
    cur = await db.execute(
        "SELECT role FROM project_members WHERE project_id = ? AND username = ?",
        (project_id, username),
    )
    row = await cur.fetchone()
    return cast(str | None, row["role"] if row else None)


async def get_kv(db: aiosqlite.Connection, key: str) -> str | None:
    cur = await db.execute("SELECT value FROM kv WHERE key = ?", (key,))
    row = await cur.fetchone()
    return cast(str | None, row["value"] if row else None)


async def set_kv(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value),
    )
    await db.commit()
