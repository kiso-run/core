"""Database initialization and migrations."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from .shared import SCHEMA


async def init_db(db_path: Path) -> aiosqlite.Connection:
    """Create tables, run migrations, and return a configured connection."""
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()

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

    cur = await db.execute("PRAGMA table_info(sessions)")
    sess_cols = {row[1] for row in await cur.fetchall()}
    if "project_id" not in sess_cols:
        await db.execute("ALTER TABLE sessions ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        await db.commit()

    cur = await db.execute("PRAGMA table_info(facts)")
    fact_cols = {row[1] for row in await cur.fetchall()}
    if "project_id" not in fact_cols:
        await db.execute("ALTER TABLE facts ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        await db.commit()
    if "entity_id" not in fact_cols:
        await db.execute("ALTER TABLE facts ADD COLUMN entity_id INTEGER REFERENCES entities(id)")
        await db.commit()

    cur = await db.execute("PRAGMA table_info(messages)")
    msg_cols = {row[1] for row in await cur.fetchall()}
    if "source" not in msg_cols:
        await db.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'user'")
        await db.commit()

    cur = await db.execute("PRAGMA table_info(plans)")
    plan_cols = {row[1] for row in await cur.fetchall()}
    if "install_proposal" not in plan_cols:
        await db.execute("ALTER TABLE plans ADD COLUMN install_proposal BOOLEAN DEFAULT 0")
        await db.commit()

    cur = await db.execute("SELECT DISTINCT tag FROM fact_tags WHERE tag LIKE 'entity:%'")
    entity_tags = await cur.fetchall()
    if entity_tags:
        from .knowledge import find_or_create_entity

        for row in entity_tags:
            tag = row[0]
            name = tag[len("entity:"):]
            entity_id = await find_or_create_entity(db, name, "wrapper")
            await db.execute(
                "UPDATE facts SET entity_id = ? WHERE id IN "
                "(SELECT fact_id FROM fact_tags WHERE tag = ?)",
                (entity_id, tag),
            )
            await db.execute("DELETE FROM fact_tags WHERE tag = ?", (tag,))
        await db.commit()

    # M1308: Migrate legacy "tool" → "wrapper" in stored data
    await db.execute("UPDATE facts SET category = 'wrapper' WHERE category = 'tool'")
    await db.execute("UPDATE entities SET kind = 'wrapper' WHERE kind = 'tool'")
    await db.execute("UPDATE tasks SET type = 'wrapper' WHERE type = 'tool'")
    await db.commit()

    return db
