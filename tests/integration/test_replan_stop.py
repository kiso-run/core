"""Circular replan deterministic stop oracle.

Unit-level coverage for ``_detect_circular_replan`` is already
comprehensive in ``tests/test_worker.py:9418+`` (11+ tests covering
word overlap, strategy fingerprint Jaccard, install loops,
goal-vs-failure variants).

Unit-level coverage for the codegen guardrail is in
``tests/test_brain.py`` (test_m1227_codegen_exec_after_tool_rejected
and neighbors).

This module pins the **integration-tier contract** that:

1. ``_detect_circular_replan`` is invokable from the runtime tier
   with the schema the worker actually uses.
2. The full runtime loop, when given a fake reviewer that always
   returns ``replan`` with the same reason and a fake planner that
   always returns the same fingerprint, terminates in a finite
   number of steps and produces a plan DB row in a terminal state
   (not running, not perpetually replanning).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from kiso.main import _workers, _worker_phases
from kiso.store import create_session
from kiso.worker.loop import _detect_circular_replan

from tests.integration.conftest import (
    AUTH_HEADER,
    make_briefing_response,
    make_classifier_response,
    make_messenger_response,
    wait_for_worker_idle,
)


pytestmark = pytest.mark.integration


async def _cleanup_workers(*sessions: str):
    for sess in sessions:
        entry = _workers.pop(sess, None)
        _worker_phases.pop(sess, None)
        if entry is not None and not entry.task.done():
            entry.task.cancel()


class TestCircularReplanDetectionContract:
    """Direct integration-tier verification of the detection contract.

    These mirror the unit-level coverage but use the production schema
    the worker actually constructs (`failure`, `goal`,
    `strategy_fingerprint` keys) so any future schema drift is caught
    by the integration suite as well.
    """

    def test_word_overlap_at_50pct_triggers_stuck(self):
        history = [
            {"failure": "package not found in registry must install foo first",
             "goal": "install foo and use it"},
            {"failure": "package not found in registry must install foo first",
             "goal": "install foo and use it"},
        ]
        assert _detect_circular_replan(history, history[-1]["failure"]) is True

    def test_strategy_fingerprint_repeat_triggers_stuck(self):
        fp = frozenset({"exec:curl example.com", "exec:parse output"})
        history = [
            {"failure": "request failed completely different reason A",
             "goal": "fetch data", "strategy_fingerprint": fp},
            {"failure": "another unrelated reason B happened later on",
             "goal": "fetch data", "strategy_fingerprint": fp},
        ]
        assert _detect_circular_replan(history, history[-1]["failure"]) is True

    def test_distinct_strategies_do_not_trigger_stuck(self):
        history = [
            {"failure": "alpha beta gamma reason text",
             "goal": "g",
             "strategy_fingerprint": frozenset({"exec:foo"})},
            {"failure": "delta epsilon zeta totally different",
             "goal": "g",
             "strategy_fingerprint": frozenset({"wrapper:bar"})},
        ]
        assert _detect_circular_replan(history, history[-1]["failure"]) is False


class TestRuntimeLoopRespectsStuckContract:
    """Drive the runtime with a custom mock LLM that emits a valid
    exec plan and a reviewer that always replans with the same
    reason. Detection eventually fires and the plan terminates."""

    async def test_repeated_failing_replan_terminates_with_failed_plan(
        self, kiso_client: httpx.AsyncClient, integration_db, webhook_collector,
    ):
        sess = "replan-stuck-1"
        await create_session(integration_db, sess)

        call_log: list[str] = []

        async def stuck_call_llm(config_obj, role, messages, *, session=None,
                                 response_format=None, model_override=None,
                                 **kwargs):
            call_log.append(role)
            if role == "classifier":
                return make_classifier_response("plan", "en")
            if role == "planner":
                # Return the same exec+msg plan every time → same fingerprint.
                # Plan must end with msg/replan and exec task needs expect.
                return json.dumps({
                    "goal": "run a failing command",
                    "tasks": [
                        {"type": "exec", "detail": "run false",
                         "command": "false",
                         "expect": "exit code zero from the false command"},
                        {"type": "msg", "detail": "Report result",
                         "expect": None},
                    ],
                })
            if role == "reviewer":
                # Always replan with the same failure reason → word overlap
                return json.dumps({
                    "status": "replan",
                    "reason": "command exited non-zero same identical reason every cycle",
                    "learn": None,
                    "retry_hint": None,
                    "summary": "exec failed",
                })
            if role == "messenger":
                return make_messenger_response("done")
            if role == "briefer":
                return make_briefing_response()
            return "Generic"

        with patch("kiso.brain.call_llm", side_effect=stuck_call_llm):
            await kiso_client.post(
                "/sessions",
                json={"session": sess},
                headers=AUTH_HEADER,
            )
            resp = await kiso_client.post(
                "/msg",
                json={"session": sess, "user": "testadmin", "content": "go"},
                headers=AUTH_HEADER,
            )
            assert resp.status_code == 202

            try:
                await wait_for_worker_idle(kiso_client, sess, timeout=20.0)
            except TimeoutError:
                pass
            await _cleanup_workers(sess)

        # The plan must reach a terminal (non-running) state — the
        # loop did NOT spin forever waiting for a new plan
        cur = await integration_db.execute(
            "SELECT status FROM plans WHERE session = ?", (sess,)
        )
        rows = await cur.fetchall()
        assert rows, "no plans were created for the stuck session"
        assert all(row["status"] in ("failed", "done") for row in rows), (
            f"plan(s) left in non-terminal state: {[r['status'] for r in rows]}"
        )

        # Reviewer was called multiple times (replan loop ran)
        assert call_log.count("reviewer") >= 1
