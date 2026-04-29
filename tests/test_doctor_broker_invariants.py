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

from unittest.mock import patch

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
            type TEXT NOT NULL,
            output TEXT
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
    task_outputs: list[str | None] | None = None,
) -> int:
    """Seed one plan row + its tasks + the user message that triggered it.

    *task_outputs* — when provided, parallel to *task_types*, sets the
    `output` column for each task (used to gate the M1597 LLM fallback).
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO plans (session, install_proposal, awaits_input) "
            "VALUES (?, ?, ?)",
            (session, int(install_proposal), int(awaits_input)),
        )
        plan_id = cur.lastrowid
        types = task_types or []
        outputs = task_outputs or [None] * len(types)
        for t, out in zip(types, outputs):
            conn.execute(
                "INSERT INTO tasks (plan_id, type, output) VALUES (?, ?, ?)",
                (plan_id, t, out),
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


_LONG_MSG_NO_KEYWORD = (
    "please diarize this audio file into separate speakers right now"
)
_SHORT_MSG_NO_KEYWORD = "diarize this audio"


class TestExecOnCapabilityLlmFallback:
    """M1597 — LLM fallback for the capability-intent heuristic.

    The 28-keyword heuristic misses long-tail capabilities (e.g.
    *diarize*, *sentiment-score*). When the heuristic misses AND the
    user message is >5 words AND the plan's exec produced output,
    `check_broker_invariants` calls the LLM fallback to classify the
    intent. Heuristic hits short-circuit so we don't pay an LLM call
    on every doctor invocation.
    """

    def test_heuristic_hit_short_circuits_no_llm_call(self, tmp_path):
        """Existing heuristic match must NOT trigger the LLM call."""
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], task_outputs=["some output"],
            user_msg="please transcribe my audio file thanks",
        )
        ctx = _ctx(tmp_path)
        with patch(
            "cli.doctor._classify_capability_intent_llm"
        ) as mock_llm:
            results = check_broker_invariants(ctx)
        assert mock_llm.call_count == 0, (
            "heuristic hit must short-circuit; LLM call wastes tokens"
        )
        target = next(
            r for r in results if r.name == "exec_on_capability_intent"
        )
        assert target.status == "warn"

    def test_heuristic_miss_long_msg_with_output_invokes_llm_yes(self, tmp_path):
        """Long-tail capability + exec output → LLM fallback runs and counts."""
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], task_outputs=["transcribed: hello world"],
            user_msg=_LONG_MSG_NO_KEYWORD,
        )
        ctx = _ctx(tmp_path)
        with patch(
            "cli.doctor._classify_capability_intent_llm",
            return_value=True,
        ) as mock_llm:
            results = check_broker_invariants(ctx)
        assert mock_llm.call_count == 1
        target = next(
            r for r in results if r.name == "exec_on_capability_intent"
        )
        assert target.status == "warn"

    def test_heuristic_miss_long_msg_no_output_no_llm_call(self, tmp_path):
        """No exec output → fallback is suppressed; doctor reports ok."""
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], task_outputs=[None],
            user_msg=_LONG_MSG_NO_KEYWORD,
        )
        ctx = _ctx(tmp_path)
        with patch(
            "cli.doctor._classify_capability_intent_llm",
            return_value=True,
        ) as mock_llm:
            results = check_broker_invariants(ctx)
        assert mock_llm.call_count == 0
        target = next(
            r for r in results if r.name == "exec_on_capability_intent"
        )
        assert target.status == "ok"

    def test_short_msg_no_keyword_no_llm_call(self, tmp_path):
        """≤5 words → fallback is suppressed even with exec output."""
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], task_outputs=["x"],
            user_msg=_SHORT_MSG_NO_KEYWORD,
        )
        ctx = _ctx(tmp_path)
        with patch(
            "cli.doctor._classify_capability_intent_llm",
            return_value=True,
        ) as mock_llm:
            results = check_broker_invariants(ctx)
        assert mock_llm.call_count == 0
        target = next(
            r for r in results if r.name == "exec_on_capability_intent"
        )
        assert target.status == "ok"

    def test_llm_returns_no_does_not_count(self, tmp_path):
        """LLM says it's not a capability request → don't count, status ok."""
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], task_outputs=["1234"],
            user_msg=_LONG_MSG_NO_KEYWORD,
        )
        ctx = _ctx(tmp_path)
        with patch(
            "cli.doctor._classify_capability_intent_llm",
            return_value=False,
        ) as mock_llm:
            results = check_broker_invariants(ctx)
        assert mock_llm.call_count == 1
        target = next(
            r for r in results if r.name == "exec_on_capability_intent"
        )
        assert target.status == "ok"

    def test_llm_returns_none_safe_default_skips(self, tmp_path):
        """LLM error / malformed → safe default: skip the count, don't crash."""
        _build_db(tmp_path)
        _seed_plan(
            tmp_path / "store.db", session="s1",
            task_types=["exec"], task_outputs=["1234"],
            user_msg=_LONG_MSG_NO_KEYWORD,
        )
        ctx = _ctx(tmp_path)
        with patch(
            "cli.doctor._classify_capability_intent_llm",
            return_value=None,
        ) as mock_llm:
            results = check_broker_invariants(ctx)
        assert mock_llm.call_count == 1
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
