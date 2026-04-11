"""Learning, fact, and knowledge-query helpers."""

from __future__ import annotations

import re
from typing import cast

import aiosqlite

from .sessions import _fact_session_filter, get_facts
from .shared import (
    _SENSITIVE_PATTERN,
    _rows_to_dicts,
    _row_to_dict,
    _update_field,
    _word_overlap_ratio,
    log,
)


def _fts5_query(text: str) -> str:
    """Tokenize *text* into a valid FTS5 OR-query.

    M1303 Bug A: the regex must mirror what the FTS5 unicode61 tokenizer
    actually treats as a word character. The default unicode61 splits on
    `.`, `/`, `-` (and any other non-word, non-underscore character), so
    those characters MUST be excluded from the regex character class. The
    previous regex `[A-Za-z0-9_./-]+` kept them inside tokens, producing
    queries like `guidance.studio` that FTS5 rejects with
    `syntax error near "."`. The exception was silently swallowed by
    callers, returning 0 facts for any query containing a dotted/slashed
    identifier — F13 was the visible symptom, but the bug affected every
    file path, version string, URL, and dotted module name in the
    codebase.
    """
    tokens = [t.strip() for t in re.findall(r"[A-Za-z0-9_]+", text)]
    if not tokens:
        return ""
    return " OR ".join(dict.fromkeys(tokens))


async def search_facts(
    db: aiosqlite.Connection,
    query: str,
    *,
    session: str | None = None,
    is_admin: bool = False,
    limit: int = 15,
    username: str | None = None,
    project_id: int | None = None,
) -> list[dict]:
    q = _fts5_query(query)
    if not q:
        return await get_facts(db, session=session, is_admin=is_admin, username=username, project_id=project_id)
    filt, params = _fact_session_filter(is_admin, session, prefix="f.", username=username, project_id=project_id)
    try:
        cur = await db.execute(
            "SELECT f.* FROM facts f "
            "JOIN kiso_facts_fts fts ON fts.rowid = f.id "
            f"WHERE kiso_facts_fts MATCH ?{filt} "
            "ORDER BY rank LIMIT ?",
            [q] + params + [limit],
        )
        results = await _rows_to_dicts(cur)
    except Exception as exc:
        log.debug("FTS5 search failed, falling back to full scan: %s", exc, exc_info=True)
        return await get_facts(db, session=session, is_admin=is_admin, username=username, project_id=project_id)
    if not results:
        return await get_facts(db, session=session, is_admin=is_admin, username=username, project_id=project_id)
    return results


async def save_learning(
    db: aiosqlite.Connection,
    content: str,
    session: str,
    user: str | None = None,
) -> int:
    if not isinstance(content, str):
        raise TypeError(
            f"save_learning: content must be str, got {type(content).__name__!r}"
        )
    if not content.strip():
        return 0
    if _SENSITIVE_PATTERN.search(content):
        log.warning("Learning rejected (contains secret-like content): %s", content[:80])
        return 0
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
    return cast(int, cur.lastrowid)


async def get_pending_learnings(
    db: aiosqlite.Connection, limit: int = 50,
) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM learnings WHERE status = 'pending' ORDER BY id LIMIT ?",
        (limit,),
    )
    return await _rows_to_dicts(cur)


async def update_learning(
    db: aiosqlite.Connection, learning_id: int, status: str,
) -> None:
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
    cur = await db.execute(
        "INSERT INTO facts (content, source, session, category, confidence, entity_id, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (content, source, session, category, confidence, entity_id, project_id),
    )
    fact_id = cast(int, cur.lastrowid)
    if tags:
        await db.executemany(
            "INSERT OR IGNORE INTO fact_tags (fact_id, tag) VALUES (?, ?)",
            [(fact_id, t) for t in tags],
        )
    await db.commit()
    return fact_id


async def save_facts_batch(db: aiosqlite.Connection, facts: list[dict]) -> None:
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


async def save_fact_tags(db: aiosqlite.Connection, fact_id: int, tags: list[str]) -> None:
    if not tags:
        return
    await db.executemany(
        "INSERT OR IGNORE INTO fact_tags (fact_id, tag) VALUES (?, ?)",
        [(fact_id, t) for t in tags],
    )
    await db.commit()


async def get_all_tags(db: aiosqlite.Connection) -> list[str]:
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
    return await _rows_to_dicts(cur)


def _normalize_entity_name(name: str) -> str:
    n = name.lower().strip()
    for prefix in ("https://", "http://", "www."):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n.rstrip("/")


async def find_or_create_entity(
    db: aiosqlite.Connection, name: str, kind: str,
) -> int:
    canonical = _normalize_entity_name(name)
    cur = await db.execute("SELECT id, kind FROM entities WHERE name = ?", (canonical,))
    existing = await cur.fetchone()
    if existing:
        if existing["kind"] != kind:
            await db.execute(
                "UPDATE entities SET kind = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (kind, existing["id"]),
            )
            await db.commit()
            log.info("Entity '%s' kind updated: %s → %s", canonical, existing["kind"], kind)
        return cast(int, existing["id"])
    cur = await db.execute(
        "INSERT INTO entities (name, kind) VALUES (?, ?)", (canonical, kind),
    )
    await db.commit()
    return cast(int, cur.lastrowid)


async def get_all_entities(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute("SELECT id, name, kind FROM entities ORDER BY name")
    return await _rows_to_dicts(cur)


async def search_facts_by_entity(
    db: aiosqlite.Connection, entity_id: int,
) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM facts WHERE entity_id = ? ORDER BY last_used DESC, id DESC",
        (entity_id,),
    )
    return await _rows_to_dicts(cur)


async def _search_facts_by_entity_tags(
    db: aiosqlite.Connection,
    *,
    entity_id: int | None,
    tags: list[str] | None,
    session_filter: str,
    session_params: list,
    fetch_limit: int,
) -> list[dict]:
    params: list = []
    select_parts = ["f.*"]
    join_parts: list[str] = []
    if entity_id is not None:
        select_parts.append("CASE WHEN f.entity_id = ? THEN 10 ELSE 0 END AS entity_score")
        params.append(entity_id)
    else:
        select_parts.append("0 AS entity_score")
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
    return await _rows_to_dicts(cur)


async def _search_facts_by_keywords(
    db: aiosqlite.Connection,
    keywords: list[str],
    *,
    session_filter: str,
    session_params: list,
    fetch_limit: int,
) -> list[dict]:
    q = _fts5_query(" ".join(keywords))
    if not q:
        return []
    try:
        cur = await db.execute(
            "SELECT f.*, 0 AS entity_score, 0 AS tag_score "
            "FROM facts f JOIN kiso_facts_fts fts ON fts.rowid = f.id "
            f"WHERE kiso_facts_fts MATCH ?{session_filter} "
            "ORDER BY rank LIMIT ?",
            [q] + session_params + [fetch_limit],
        )
        return await _rows_to_dicts(cur)
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
    project_id: int | None = None,
) -> list[dict]:
    if not entity_id and not tags and not keywords:
        return []
    session_filter, session_params = _fact_session_filter(
        is_admin, session, prefix="f.", username=username, project_id=project_id,
    )
    sp = list(session_params)
    fetch_limit = limit * 2
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
        if not rows and keywords:
            rows = await _search_facts_by_keywords(
                db, keywords, session_filter=session_filter, session_params=sp, fetch_limit=fetch_limit,
            )
    else:
        rows = await _search_facts_by_keywords(
            db, keywords or [], session_filter=session_filter, session_params=sp, fetch_limit=fetch_limit,
        )
    kw_set = {w.lower() for w in (keywords or [])} if keywords else set()
    scored: list[tuple[int, dict]] = []
    for row in rows:
        base = row.get("entity_score", 0) + row.get("tag_score", 0)
        if kw_set:
            content_lower = row["content"].lower()
            base += sum(1 for kw in kw_set if kw in content_lower)
        scored.append((base, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    results: list[dict] = []
    for _, row in scored[:limit]:
        row.pop("entity_score", None)
        row.pop("tag_score", None)
        row.pop("tag_count", None)
        results.append(row)
    return results


async def backfill_fact_entities(db: aiosqlite.Connection) -> int:
    entities = await get_all_entities(db)
    if not entities:
        return 0
    orphan_cur = await db.execute("SELECT id, content FROM facts WHERE entity_id IS NULL")
    orphans = await orphan_cur.fetchall()
    if not orphans:
        return 0
    updated = 0
    for row in orphans:
        content_lower = row["content"].lower()
        for entity in entities:
            if re.search(r"\b" + re.escape(entity["name"]) + r"\b", content_lower):
                await db.execute(
                    "UPDATE facts SET entity_id = ? WHERE id = ?",
                    (entity["id"], row["id"]),
                )
                updated += 1
                break
    if updated:
        await db.commit()
    return updated


async def delete_facts(db: aiosqlite.Connection, fact_ids: list[int]) -> None:
    if not fact_ids:
        return
    placeholders = ",".join("?" for _ in fact_ids)
    await db.execute(f"DELETE FROM facts WHERE id IN ({placeholders})", fact_ids)
    await db.commit()


async def update_fact_usage(db: aiosqlite.Connection, fact_ids: list[int]) -> None:
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
    cur = await db.execute(
        "SELECT id, content FROM facts WHERE category = 'safety' ORDER BY created_at",
    )
    return await _rows_to_dicts(cur)


async def get_behavior_facts(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute(
        "SELECT id, content FROM facts WHERE category = 'behavior' ORDER BY created_at",
    )
    return await _rows_to_dicts(cur)


async def list_knowledge(
    db: aiosqlite.Connection,
    *,
    category: str | None = None,
    entity: str | None = None,
    tag: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if search:
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
                rows = await _rows_to_dicts(cur)
            except Exception:
                rows = []
            if rows:
                return await _attach_tags(db, rows)
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
    rows = await _rows_to_dicts(cur)
    return await _attach_tags(db, rows)


async def _attach_tags(db: aiosqlite.Connection, rows: list[dict]) -> list[dict]:
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
    cur = await db.execute(
        "UPDATE facts SET confidence = MAX(0.0, confidence - ?) "
        "WHERE COALESCE(last_used, created_at) < datetime('now', ?) "
        "AND category != 'safety'",
        (decay_rate, f"-{decay_days} days"),
    )
    await db.commit()
    return cur.rowcount


async def archive_low_confidence_facts(
    db: aiosqlite.Connection, threshold: float = 0.3,
) -> int:
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


async def update_fact_content(
    db: aiosqlite.Connection, fact_id: int, content: str,
) -> None:
    await db.execute("UPDATE facts SET content = ? WHERE id = ?", (content, fact_id))
    await db.commit()
