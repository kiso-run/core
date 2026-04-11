"""Database initialization and migrations."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from .shared import SCHEMA


async def init_db(db_path: Path) -> aiosqlite.Connection:
    """Create tables and return a configured connection.

    The schema in shared.py is the single source of truth.
    No migrations — the software is pre-release.
    """
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    return db
