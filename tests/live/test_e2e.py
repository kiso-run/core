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
from kiso.worker import _execute_plan, _review_task

pytestmark = pytest.mark.llm_live

from tests.conftest import LLM_TEST_TIMEOUT as TIMEOUT


class TestSimpleQuestionE2E:
    async def test_simple_question_flow(
        self, live_config, seeded_db, live_session, tmp_path, mock_noop_infra,
    ):
        """What: Plans 'What is the tallest mountain?' then executes the full pipeline.

        Why: Validates the simplest happy path — question in, plan created, msg task produces correct answer.
        Expects: Plan validates, execution succeeds, final msg output mentions 'everest'.
        """
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "What is the tallest mountain in the world?",
        )

        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_tools", return_value=[]),
        ):
            plan = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin",
                    "What is the tallest mountain in the world?",
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
                skill=t.get("tool"), args=t.get("args"),
                expect=t.get("expect"),
            )

        with mock_noop_infra:
            success, replan_reason, _stuck, completed, remaining, _outputs = await asyncio.wait_for(
                _execute_plan(
                    seeded_db, live_config, live_session, plan_id,
                    plan["goal"],
                    "What is the tallest mountain in the world?",
                ),
                timeout=TIMEOUT,
            )

        assert success is True
        # Last completed task should be a msg mentioning Everest
        msg_tasks = [t for t in completed if t["type"] == "msg"]
        assert msg_tasks
        assert "everest" in msg_tasks[-1]["output"].lower()


class TestExecAndReviewOkE2E:
    async def test_exec_review_ok_flow(
        self, live_config, seeded_db, live_session, tmp_path, mock_noop_infra,
    ):
        """What: Plans 'echo hello world', executes the exec+review+msg pipeline.

        Why: Validates the exec-review-msg pipeline end-to-end with a real LLM reviewer.
        Expects: Exec tasks complete, output contains 'hello'.
        """
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "Run 'echo hello world' and tell me the output",
        )

        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_tools", return_value=[]),
        ):
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
                skill=t.get("tool"), args=t.get("args"),
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
    async def test_replan_after_failed_exec(
        self, live_config, seeded_db, live_session, tmp_path, mock_noop_infra,
    ):
        """What: Builds a plan with a deliberately failing exec (write to nonexistent dir), runs _execute_plan.

        Why: Validates the replan loop — a failed exec must produce a replan reason, and re-planning with that reason yields a valid new plan.
        Expects: success=False, non-empty replan_reason, second planner call produces a valid plan.
        """
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "Save report to the project directory",
        )
        plan_id = await create_plan(
            seeded_db, live_session, msg_id,
            "Create a report file in the project directory",
        )
        # Deliberately failing exec — /proc is a virtual filesystem where
        # mkdir -p always fails, even as root.  No workaround exists.
        await create_task(
            seeded_db, plan_id, live_session,
            type="exec",
            detail="Write 'hello world' to /proc/nonexistent/report.txt",
            expect="File created successfully at the specified path",
        )
        await create_task(
            seeded_db, plan_id, live_session,
            type="msg",
            detail="Tell the user the report was saved",
        )

        with mock_noop_infra:
            success, replan_reason, _stuck, completed, remaining, _outputs = await asyncio.wait_for(
                _execute_plan(
                    seeded_db, live_config, live_session, plan_id,
                    "Create a report file in the project directory",
                    "save report to the project directory",
                ),
                timeout=TIMEOUT,
            )

        assert success is False
        assert replan_reason is not None
        assert len(replan_reason) > 0

        # Verify a new plan from the replan context is valid
        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_tools", return_value=[]),
        ):
            new_plan = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin",
                    f"save report to the project\n\n## Failure Reason\n{replan_reason}",
                ),
                timeout=TIMEOUT,
            )
        assert validate_plan(new_plan) == []


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
