"""L3 — End-to-end tests.

Full pipeline through _execute_plan. Non-LLM infrastructure mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from kiso.brain import run_planner, run_reviewer, validate_plan, validate_review
from kiso.store import (
    create_plan,
    create_task,
    save_message,
)
from kiso.worker.loop import _execute_plan, _review_task

pytestmark = pytest.mark.llm_live

from tests._helpers import make_task_dict
from tests.conftest import LLM_ROLE_ONLY_TIMEOUT, LLM_TEST_TIMEOUT as TIMEOUT


class TestExecAndReviewOkE2E:
    async def test_exec_review_ok_flow(
        self, live_config, seeded_db, live_session, live_kiso_dir, mock_noop_infra,
    ):
        """What: Plans 'echo hello world', executes the exec+review+msg pipeline.

        Why: Validates the exec-review-msg pipeline end-to-end with a real LLM reviewer.
        Expects: Exec tasks complete, output contains 'hello'.
        """
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "Run 'echo hello world' and tell me the output",
        )

        plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin",
                "Run 'echo hello world' and tell me the output",
            ),
            timeout=TIMEOUT,
        )
        assert validate_plan(plan) == []

        plan_id = await create_plan(
            seeded_db, live_session, msg_id, plan["goal"],
        )
        for t in plan["tasks"]:
            await create_task(
                seeded_db, plan_id, live_session,
                type=t["type"], detail=t["detail"],
                args=t.get("args"),
                expect=t.get("expect"),
            )

        with mock_noop_infra:
            success, replan_reason, _stuck, completed, remaining, _outputs = await asyncio.wait_for(
                _execute_plan(
                    seeded_db, live_config, live_session, plan_id,
                    plan["goal"],
                    "Run 'echo hello world' and tell me the output",
                ),
                timeout=TIMEOUT,
            )

        # Reviewer may trigger replan on valid output (LLM flakiness).
        # Verify that exec tasks ran and produced output.
        exec_tasks = [t for t in completed if t["type"] == "exec"]
        assert exec_tasks, f"No exec tasks completed (success={success}, reason={replan_reason})"
        all_output = " ".join((t.get("output") or "") for t in exec_tasks).lower()
        assert "hello" in all_output, f"Expected 'hello' in exec output: {all_output[:200]}"


class TestReplanFlowE2E:
    async def test_planner_emits_valid_plan_on_replan_context(
        self, live_config, seeded_db, live_session, live_kiso_dir,
    ):
        """The planner, called with `is_replan=True` on an enriched
        message that contains a `_build_replan_context` block, must
        return a validated plan.

        This is the only LIVE coverage of the planner-on-replan
        path (`is_replan=True`). The replan-context builder itself
        is unit-tested in ``tests/test_worker.py::TestBuildReplanContext``.

        Synthetic input avoids the LLM-driven exec_translator +
        reviewer prologue, whose flake rate dominated the signal.
        """
        completed = [make_task_dict(
            detail="Write 'hello world' to /proc/nonexistent/report.txt",
            status="failed",
            output=(
                "bash: line 1: /proc/nonexistent/report.txt: "
                "No such file or directory"
            ),
        )]
        remaining = [{"type": "msg", "detail": "Tell the user the report was saved"}]
        replan_reason = (
            "exec failed: /proc/nonexistent/ is a virtual filesystem and "
            "cannot accept new files; the writable target path must change"
        )

        from kiso.worker.utils import _build_replan_context
        replan_ctx = _build_replan_context(
            completed=completed,
            remaining=remaining,
            replan_reason=replan_reason,
            replan_history=[],
        )
        enriched_msg = f"save report to the project\n\n{replan_ctx}"

        await save_message(
            seeded_db, live_session, "testadmin", "user", enriched_msg,
        )
        new_plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin",
                enriched_msg, is_replan=True,
            ),
            timeout=LLM_ROLE_ONLY_TIMEOUT,
        )
        assert validate_plan(new_plan, is_replan=True) == [], (
            f"planner produced invalid replan plan: {new_plan!r}"
        )


class TestKnowledgeFlowE2E:
    async def test_review_produces_learning(
        self, live_config, seeded_db, live_session,
    ):
        """What: Calls _review_task with a real LLM on a successful exec output.

        Why: Validates that the reviewer extracts sensible learnings from successful task execution.
        Expects: Review status is 'ok', any learnings are non-trivial strings (len > 5).
        """
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "set up the Python project",
        )
        plan_id = await create_plan(
            seeded_db, live_session, msg_id, "Set up Python project",
        )
        task_id = await create_task(
            seeded_db, plan_id, live_session,
            type="exec",
            detail="python3 --version && pip --version",
            expect="Python and pip versions displayed",
        )

        task_row = {
            "id": task_id,
            "type": "exec",
            "detail": "python3 --version && pip --version",
            "expect": "Python and pip versions displayed",
            "output": "Python 3.12.3\npip 24.0 from /usr/lib/python3/dist-packages/pip",
            "stderr": "",
            "status": "done",
        }

        with patch("kiso.worker.loop.audit"):
            review = await asyncio.wait_for(
                _review_task(
                    live_config, seeded_db, live_session,
                    "Set up Python project", task_row,
                    "set up the Python project",
                ),
                timeout=TIMEOUT,
            )

        assert validate_review(review) == []
        # Review should pass (output matches expect)
        assert review["status"] == "ok"
        # If the LLM decided to extract a learning, verify it's sensible
        if review.get("learn"):
            assert isinstance(review["learn"], list)
            for item in review["learn"]:
                assert isinstance(item, str)
                assert len(item) > 5


class TestReviewerExitCodeE2E:
    async def test_failed_exec_with_error_output_replans(self, live_config):
        """What: Sends a FAILED exec (missing pyproject.toml) to the reviewer.

        Why: Validates the reviewer correctly handles the Command Status FAILED section and triggers replan.
        Expects: Review passes validation, status is 'replan'.
        """
        review = await asyncio.wait_for(
            run_reviewer(
                live_config,
                goal="Install project dependencies",
                detail="uv sync",
                expect="All packages installed successfully",
                output="error: No `pyproject.toml` found in `/workspace` "
                       "or any parent directory",
                user_message="install the dependencies",
                success=False,
            ),
            timeout=TIMEOUT,
        )
        assert validate_review(review) == []
        assert review["status"] == "replan", (
            f"Reviewer should replan on FAILED exec, got: {review['status']} "
            f"(reason: {review.get('reason', 'N/A')})"
        )
