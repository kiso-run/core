"""Database initialization and migrations."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from .shared import SCHEMA


# Idempotent column-add migrations. Each entry is (table, column, definition).
# `init_db` runs `ALTER TABLE ADD COLUMN` for any column missing on an existing
# table. New databases get the column from `SCHEMA` directly; this list only
# matters for upgrading databases created by an older release.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("plans", "awaits_input", "INTEGER DEFAULT 0"),  # M1579a
)


async def _ensure_columns(db: aiosqlite.Connection) -> None:
    for table, column, definition in _COLUMN_MIGRATIONS:
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )


async def init_db(db_path: Path) -> aiosqlite.Connection:
    """Create tables and return a configured connection.

    The schema in shared.py is the single source of truth for new
    databases. For existing databases, `_ensure_columns` runs
    idempotent ALTER TABLE statements so column additions land
    without data loss.
    """
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await _ensure_columns(db)
    await db.commit()
    return db
