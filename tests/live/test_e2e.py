"""L3 — End-to-end tests.

Full pipeline through _execute_plan. Non-LLM infrastructure mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from kiso.brain import run_planner, validate_plan, validate_review
from kiso.store import (
    create_plan,
    create_task,
    save_message,
)
from kiso.worker import _execute_plan, _review_task

pytestmark = pytest.mark.llm_live

TIMEOUT = 90


class TestSimpleQuestionE2E:
    async def test_simple_question_flow(
        self, live_config, seeded_db, live_session, tmp_path, mock_noop_infra,
    ):
        """Full flow: plan a simple question → execute → get answer."""
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "What is the tallest mountain in the world?",
        )

        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_skills", return_value=[]),
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
                skill=t.get("skill"), args=t.get("args"),
                expect=t.get("expect"),
            )

        with mock_noop_infra:
            success, replan_reason, completed, remaining = await asyncio.wait_for(
                _execute_plan(
                    seeded_db, live_config, live_session, plan_id,
                    plan["goal"],
                    "What is the tallest mountain in the world?",
                    exec_timeout=60,
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
        """Plan with echo → exec → review ok → msg."""
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "Run 'echo hello world' and tell me the output",
        )

        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_skills", return_value=[]),
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
                skill=t.get("skill"), args=t.get("args"),
                expect=t.get("expect"),
            )

        with mock_noop_infra:
            success, replan_reason, completed, remaining = await asyncio.wait_for(
                _execute_plan(
                    seeded_db, live_config, live_session, plan_id,
                    plan["goal"],
                    "Run 'echo hello world' and tell me the output",
                    exec_timeout=60,
                ),
                timeout=TIMEOUT,
            )

        assert success is True
        types_completed = [t["type"] for t in completed]
        assert "exec" in types_completed
        assert "msg" in types_completed


class TestReplanFlowE2E:
    async def test_replan_after_failed_exec(
        self, live_config, seeded_db, live_session, tmp_path, mock_noop_infra,
    ):
        """Manually-built failing plan → _execute_plan → replan reason."""
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user",
            "List files in the project",
        )
        plan_id = await create_plan(
            seeded_db, live_session, msg_id,
            "List files in the project directory",
        )
        # Deliberately failing exec command
        await create_task(
            seeded_db, plan_id, live_session,
            type="exec",
            detail="ls /absolutely_nonexistent_dir_xyz_12345",
            expect="Directory listing with files",
        )
        await create_task(
            seeded_db, plan_id, live_session,
            type="msg",
            detail="Tell the user the files found",
        )

        with mock_noop_infra:
            success, replan_reason, completed, remaining = await asyncio.wait_for(
                _execute_plan(
                    seeded_db, live_config, live_session, plan_id,
                    "List files in the project directory",
                    "list files in the project",
                    exec_timeout=60,
                ),
                timeout=TIMEOUT,
            )

        assert success is False
        assert replan_reason is not None
        assert len(replan_reason) > 0

        # Verify a new plan from the replan context is valid
        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_skills", return_value=[]),
        ):
            new_plan = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin",
                    f"list files in the project\n\n## Failure Reason\n{replan_reason}",
                ),
                timeout=TIMEOUT,
            )
        assert validate_plan(new_plan) == []


class TestKnowledgeFlowE2E:
    async def test_review_produces_learning(
        self, live_config, seeded_db, live_session,
    ):
        """_review_task with real LLM → check if learning is stored in DB."""
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

        with patch("kiso.worker.audit"):
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
            assert isinstance(review["learn"], str)
            assert len(review["learn"]) > 0
