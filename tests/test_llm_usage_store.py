"""Unit tests for ``kiso/store/usage.py`` — the dedicated
``llm_usage`` table (M1537 follow-up).

Contract:

- ``ensure_usage_table(conn)`` creates the schema idempotently (two
  composite indexes: ``(session, ts)`` and ``(role, ts)``).
- ``record_usage(conn, …)`` inserts one row with an ISO-8601 UTC
  timestamp. Null ``cost_usd`` is allowed when pricing is unknown.
- ``query_usage(conn, since_days, group_by)`` returns aggregated
  rows sorted by ``cost_usd`` desc, with fallback to
  token totals when cost is null.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def conn() -> sqlite3.Connection:
    from kiso.store.usage import ensure_usage_table

    c = sqlite3.connect(":memory:")
    ensure_usage_table(c)
    return c


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestEnsureTable:
    def test_creates_table(self, conn: sqlite3.Connection) -> None:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(llm_usage)")]
        for name in (
            "id", "session", "role", "model",
            "prompt_tokens", "completion_tokens", "cost_usd", "ts",
        ):
            assert name in cols, f"expected column {name!r}"

    def test_creates_indexes(self, conn: sqlite3.Connection) -> None:
        names = {
            r[1] for r in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='llm_usage'"
            )
        }
        assert "idx_usage_session_ts" in names
        assert "idx_usage_role_ts" in names

    def test_is_idempotent(self, conn: sqlite3.Connection) -> None:
        # Calling twice must not raise.
        from kiso.store.usage import ensure_usage_table
        ensure_usage_table(conn)
        ensure_usage_table(conn)


class TestRecordUsage:
    def test_inserts_row(self, conn: sqlite3.Connection) -> None:
        from kiso.store.usage import record_usage

        record_usage(
            conn,
            session="dev",
            role="planner",
            model="deepseek/deepseek-v3.2",
            prompt_tokens=1000,
            completion_tokens=250,
            cost_usd=0.0123,
        )
        conn.commit()
        rows = list(conn.execute("SELECT session, role, model, "
                                   "prompt_tokens, completion_tokens, "
                                   "cost_usd FROM llm_usage"))
        assert rows == [("dev", "planner", "deepseek/deepseek-v3.2",
                         1000, 250, 0.0123)]

    def test_allows_null_cost(self, conn: sqlite3.Connection) -> None:
        from kiso.store.usage import record_usage

        record_usage(
            conn,
            session="dev",
            role="worker",
            model="unknown/model",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=None,
        )
        conn.commit()
        (cost,) = conn.execute(
            "SELECT cost_usd FROM llm_usage"
        ).fetchone()
        assert cost is None

    def test_stamps_ts_as_iso_utc(self, conn: sqlite3.Connection) -> None:
        from kiso.store.usage import record_usage

        before = datetime.now(timezone.utc).replace(microsecond=0)
        record_usage(
            conn,
            session="s",
            role="r",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
        )
        conn.commit()
        (ts,) = conn.execute("SELECT ts FROM llm_usage").fetchone()
        # Parseable as ISO-8601 UTC (trailing Z).
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        # within a few seconds of now
        assert abs((parsed - before).total_seconds()) < 60


class TestQueryUsage:
    def _seed(self, conn: sqlite3.Connection) -> None:
        from kiso.store.usage import record_usage

        now = datetime.now(timezone.utc)

        # Recent rows
        record_usage(conn, session="dev", role="planner",
                     model="deepseek/deepseek-v3.2",
                     prompt_tokens=1000, completion_tokens=200,
                     cost_usd=0.0050,
                     ts=_iso(now))
        record_usage(conn, session="dev", role="planner",
                     model="deepseek/deepseek-v3.2",
                     prompt_tokens=500, completion_tokens=100,
                     cost_usd=0.0025,
                     ts=_iso(now))
        record_usage(conn, session="dev", role="messenger",
                     model="google/gemini-2.5-flash",
                     prompt_tokens=200, completion_tokens=50,
                     cost_usd=0.0002,
                     ts=_iso(now))
        # Old row (>10d ago)
        record_usage(conn, session="dev", role="planner",
                     model="deepseek/deepseek-v3.2",
                     prompt_tokens=9999, completion_tokens=9999,
                     cost_usd=99.99,
                     ts=_iso(now - timedelta(days=30)))
        conn.commit()

    def test_groups_by_role(self, conn: sqlite3.Connection) -> None:
        from kiso.store.usage import query_usage
        self._seed(conn)

        rows = query_usage(conn, since_days=7, group_by="role")
        # planner first (higher cost), then messenger
        assert [r["key"] for r in rows] == ["planner", "messenger"]
        planner = rows[0]
        assert planner["prompt_tokens"] == 1500
        assert planner["completion_tokens"] == 300
        assert planner["calls"] == 2
        assert planner["cost_usd"] == pytest.approx(0.0075, abs=1e-9)

    def test_respects_since_window(self, conn: sqlite3.Connection) -> None:
        from kiso.store.usage import query_usage
        self._seed(conn)

        # since_days=7 — old row (30d ago) is excluded.
        rows = query_usage(conn, since_days=7, group_by="role")
        planner_cost = next(r for r in rows if r["key"] == "planner")["cost_usd"]
        assert planner_cost < 1.0  # 99.99 would overflow if leaked

        # since_days=60 — old row included, planner cost explodes.
        rows_wide = query_usage(conn, since_days=60, group_by="role")
        planner_cost_wide = next(
            r for r in rows_wide if r["key"] == "planner"
        )["cost_usd"]
        assert planner_cost_wide > 99.0

    def test_groups_by_model(self, conn: sqlite3.Connection) -> None:
        from kiso.store.usage import query_usage
        self._seed(conn)

        rows = query_usage(conn, since_days=7, group_by="model")
        models = {r["key"] for r in rows}
        assert "deepseek/deepseek-v3.2" in models
        assert "google/gemini-2.5-flash" in models


# ────────────────────────────────────────────────────────────────────
# Hook: audit.log_llm_call also records into llm_usage if wired
# ────────────────────────────────────────────────────────────────────

class TestAuditHook:
    def test_audit_log_llm_call_also_writes_usage(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The existing audit.log_llm_call path is extended to also
        append an llm_usage row when the installed callback is set.
        """
        from kiso import audit
        from kiso.store.usage import record_usage

        def _cb(session, role, model, input_tokens, output_tokens, cost_usd):
            record_usage(
                conn,
                session=session,
                role=role,
                model=model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                cost_usd=cost_usd,
            )

        monkeypatch.setattr(audit, "_usage_recorder", _cb, raising=False)

        audit.log_llm_call(
            session="dev",
            role="planner",
            model="google/gemini-2.5-flash",
            provider="openrouter",
            input_tokens=100,
            output_tokens=20,
            duration_ms=500,
            status="ok",
        )
        conn.commit()

        (count,) = conn.execute(
            "SELECT COUNT(*) FROM llm_usage WHERE session = 'dev'"
        ).fetchone()
        assert count == 1
