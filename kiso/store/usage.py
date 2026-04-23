"""Dedicated ``llm_usage`` SQLite table for per-call token + cost
tracking.

The audit JSONL trail records the same information for raw-event
purposes (see ``kiso/audit.py``); this table is the query-optimised
view that powers ``kiso stats --costs``. Two composite indexes
let the CLI aggregate by session or by role without a full scan:

- ``idx_usage_session_ts`` — per-session views
- ``idx_usage_role_ts``    — per-role roll-ups

Cost is stored nullable: pricing may not be known for every model
(a new OpenRouter provider, a self-hosted endpoint, etc.). Null
costs are excluded from totals rather than booked as zero.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Literal

GroupBy = Literal["role", "model", "session"]


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS llm_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session            TEXT NOT NULL,
    role               TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    cost_usd           REAL,
    ts                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_session_ts ON llm_usage(session, ts);
CREATE INDEX IF NOT EXISTS idx_usage_role_ts    ON llm_usage(role, ts);
"""


def ensure_usage_table(conn: sqlite3.Connection) -> None:
    """Create ``llm_usage`` + indexes if missing. Idempotent."""
    conn.executescript(_SCHEMA)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_usage(
    conn: sqlite3.Connection,
    *,
    session: str,
    role: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float | None,
    ts: str | None = None,
) -> None:
    """Insert one llm_usage row.

    ``ts`` defaults to ``now()`` in ISO-8601 UTC. Caller is
    responsible for committing; ``record_usage`` never commits so
    it can be batched with other writes in the same transaction.
    """
    conn.execute(
        "INSERT INTO llm_usage "
        "(session, role, model, prompt_tokens, completion_tokens, "
        " cost_usd, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session, role, model,
            int(prompt_tokens), int(completion_tokens),
            cost_usd, ts or _now_iso(),
        ),
    )


def query_usage(
    conn: sqlite3.Connection,
    *,
    since_days: int,
    group_by: GroupBy,
) -> list[dict]:
    """Aggregated usage rows for the last *since_days* days.

    Rows are ``{key, calls, prompt_tokens, completion_tokens,
    cost_usd}`` and are sorted by total cost descending. ``cost_usd``
    is the sum of non-null costs in the group (calls with unknown
    price contribute tokens but no cost).
    """
    if group_by not in ("role", "model", "session"):
        raise ValueError(f"invalid group_by: {group_by!r}")

    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = cutoff.fromtimestamp(
        cutoff.timestamp() - since_days * 86400, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = conn.execute(
        f"SELECT {group_by} AS key,"
        f"       COUNT(*) AS calls,"
        f"       SUM(prompt_tokens) AS prompt_tokens,"
        f"       SUM(completion_tokens) AS completion_tokens,"
        f"       SUM(CASE WHEN cost_usd IS NULL THEN 0 ELSE cost_usd END) "
        f"         AS cost_usd "
        f"  FROM llm_usage "
        f" WHERE ts >= ? "
        f" GROUP BY {group_by} "
        f" ORDER BY cost_usd DESC, (prompt_tokens + completion_tokens) DESC",
        (cutoff,),
    )
    out: list[dict] = []
    for key, calls, pt, ct, cost in cur.fetchall():
        out.append({
            "key": key,
            "calls": int(calls),
            "prompt_tokens": int(pt or 0),
            "completion_tokens": int(ct or 0),
            "cost_usd": float(cost or 0.0),
        })
    return out
