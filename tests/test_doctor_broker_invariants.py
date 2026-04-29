"""M1592 — `kiso doctor` broker-model invariant checks.

Synthetic DB fixtures exercise each threshold of the three invariants
introduced by `cli/doctor.py::check_broker_invariants`:

1. Msg-only plans without an escape flag — `> 5%` warn, `> 20%` fail.
2. Plans that ran exec on a capability-style user message — `> 0` warn.
3. Plans with `awaits_input=true` followed by an exec plan in the same
   session — `> 0` warn.

The capability-intent heuristic is a small static keyword list; the
detection is word-boundary aware.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cli.doctor import (
    CAPABILITY_INTENT_KEYWORDS,
    DoctorContext,
    _has_capability_intent,
    check_broker_invariants,
)


def _build_db(tmp_path: Path) -> Path:
    """Create a minimal store.db schema with just the columns
    `check_broker_invariants` reads. Avoids pulling in the full kiso
    schema for a focused unit test."""
    db_path = tmp_path / "store.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT NOT NULL,
            goal TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            install_proposal INTEGER DEFAULT 0,
            awaits_input INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            type TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _seed_plan(
    db_path: Path,
    session: str = "s1",
    *,
    task_types: list[str] | None = None,
    awaits_input: bool = False,
    install_proposal: bool = False,
    user_msg: str = "",
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO plans (session, install_proposal, awaits_input) "
            "VALUES (?, ?, ?)",
            (session, int(install_proposal), int(awaits_input)),
        )
        plan_id = cur.lastrowid
        for t in task_types or []:
            conn.execute(
                "INSERT INTO tasks (plan_id, type) VALUES (?, ?)",
                (plan_id, t),
            )
        if user_msg:
            conn.execute(
                "INSERT INTO messages (session, role, content) VALUES (?, 'user', ?)",
                (session, user_msg),
            )
        conn.commit()
        return plan_id
    finally:
        conn.close()


def _ctx(tmp_path: Path) -> DoctorContext:
    return DoctorContext(
        kiso_dir=tmp_path,
        config=None,
        config_path=tmp_path / "config.toml",
        api_key="",
    )


class TestCapabilityHeuristic:
    @pytest.mark.parametrize("msg", [
        "please transcribe this audio",
        "search for python tutorials",
        "OCR the image",
        "summarize the doc",
        "Render the report",
    ])
    def test_keyword_matches(self, msg):
        assert _has_capability_intent(msg)

    @pytest.mark.parametrize("msg", [
        "hello there",
        "what is recursion",
        "transcript",  # word-boundary: not "transcribe"
        "",
    ])
    def test_keyword_misses(self, msg):
        assert not _has_capability_intent(msg)

    def test_keyword_set_is_generalist(self):
        # No MCP names, server names, or hardcoded references.
        for kw in CAPABILITY_INTENT_KEYWORDS:
            assert "mcp" not in kw.lower()
            assert "kiso" not in kw.lower()


class TestNoBrokerData:
    def test_db_missing_emits_ok(self, tmp_path):
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        assert len(results) == 1
        assert results[0].name == "db_file"
        assert results[0].status == "ok"

    def test_empty_db_emits_ok(self, tmp_path):
        _build_db(tmp_path)
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        names = {r.name for r in results}
        assert "recent_plans" in names


class TestMsgOnlyEscapeCheck:
    def test_under_threshold_is_ok(self, tmp_path):
        _build_db(tmp_path)
        # 100 healthy plans (mixed task types).
        for i in range(100):
            _seed_plan(
                tmp_path / "store.db", session=f"s{i}",
                task_types=["exec", "msg"],
            )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(r for r in results if r.name == "msg_only_no_escape")
        assert target.status == "ok"

    def test_above_warn_threshold_warns(self, tmp_path):
        _build_db(tmp_path)
        # 90 healthy + 10 bad msg-only without escape (10% > 5%).
        for i in range(90):
            _seed_plan(
                tmp_path / "store.db", session=f"good{i}",
                task_types=["exec"],
            )
        for i in range(10):
            _seed_plan(
                tmp_path / "store.db", session=f"bad{i}",
                task_types=["msg"],
            )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(r for r in results if r.name == "msg_only_no_escape")
        assert target.status == "warn"

    def test_above_fail_threshold_fails(self, tmp_path):
        _build_db(tmp_path)
        # 70 healthy + 30 bad (30% > 20%).
        for i in range(70):
            _seed_plan(
                tmp_path / "store.db", session=f"good{i}",
                task_types=["exec"],
            )
        for i in range(30):
            _seed_plan(
                tmp_path / "store.db", session=f"bad{i}",
                task_types=["msg"],
            )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(r for r in results if r.name == "msg_only_no_escape")
        assert target.status == "fail"

    def test_msg_only_with_escape_flag_passes(self, tmp_path):
        _build_db(tmp_path)
        # 100 plans, all msg-only but with awaits_input set — these are
        # legitimate broker pauses, not breaches.
        for i in range(100):
            _seed_plan(
                tmp_path / "store.db", session=f"s{i}",
                task_types=["msg"], awaits_input=True,
            )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(r for r in results if r.name == "msg_only_no_escape")
        assert target.status == "ok"


class TestExecOnCapabilityIntent:
    def test_exec_on_capability_message_warns(self, tmp_path):
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], user_msg="please transcribe my audio",
        )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(
            r for r in results if r.name == "exec_on_capability_intent"
        )
        assert target.status == "warn"

    def test_exec_on_non_capability_message_ok(self, tmp_path):
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], user_msg="ls /tmp",
        )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(
            r for r in results if r.name == "exec_on_capability_intent"
        )
        assert target.status == "ok"


class TestAwaitsInputSelfResume:
    def test_awaits_input_followed_by_exec_warns(self, tmp_path):
        _build_db(tmp_path)
        # First plan in session paused (awaits_input). Second plan in
        # same session ran exec — silent self-resume.
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["msg"], awaits_input=True,
        )
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"],
        )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(
            r for r in results if r.name == "awaits_input_self_resume"
        )
        assert target.status == "warn"

    def test_awaits_input_followed_by_msg_is_ok(self, tmp_path):
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["msg"], awaits_input=True,
        )
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["msg"], awaits_input=True,
        )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(
            r for r in results if r.name == "awaits_input_self_resume"
        )
        assert target.status == "ok"

    def test_awaits_input_in_different_sessions_not_correlated(self, tmp_path):
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="sa",
            task_types=["msg"], awaits_input=True,
        )
        # Different session — self-resume must NOT fire.
        _seed_plan(
            tmp_path / "store.db", session="sb",
            task_types=["exec"],
        )
        ctx = _ctx(tmp_path)
        results = check_broker_invariants(ctx)
        target = next(
            r for r in results if r.name == "awaits_input_self_resume"
        )
        assert target.status == "ok"
