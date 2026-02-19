"""L2 — Partial flow tests.

Connected components with 2-3 real LLM calls per test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from kiso.brain import (
    run_planner,
    run_reviewer,
    validate_plan,
    validate_review,
)
from kiso.store import (
    create_plan,
    create_task,
    save_message,
)
from kiso.worker import _msg_task

pytestmark = pytest.mark.llm_live

TIMEOUT = 90


class TestPlanAndExecuteMsg:
    async def test_plan_then_msg_execution(
        self, live_config, seeded_db, live_session, tmp_path,
    ):
        """Planner produces a plan, then _msg_task executes the final msg."""
        await save_message(seeded_db, live_session, "testadmin", "user", "hi")

        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_skills", return_value=[]),
        ):
            plan = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin",
                    "What is 2 + 2?",
                ),
                timeout=TIMEOUT,
            )

        assert validate_plan(plan) == []
        last_task = plan["tasks"][-1]
        assert last_task["type"] == "msg"

        # Execute the msg task with the real LLM
        text = await asyncio.wait_for(
            _msg_task(
                live_config, seeded_db, live_session,
                last_task["detail"],
            ),
            timeout=TIMEOUT,
        )
        assert "4" in text


class TestExecThenReviewOk:
    async def test_review_ok_on_successful_exec(self, live_config):
        """Reviewer returns ok for clearly successful output."""
        review = await asyncio.wait_for(
            run_reviewer(
                live_config,
                goal="Show the current date",
                detail="date +%Y-%m-%d",
                expect="Prints today's date in YYYY-MM-DD format",
                output="2025-01-15",
                user_message="what is today's date?",
            ),
            timeout=TIMEOUT,
        )
        assert validate_review(review) == []
        assert review["status"] == "ok"


class TestExecThenReviewReplan:
    async def test_review_replan_on_failed_exec(self, live_config):
        """Reviewer returns replan with actionable reason for failed output."""
        review = await asyncio.wait_for(
            run_reviewer(
                live_config,
                goal="Install project dependencies",
                detail="pip install -r requirements.txt",
                expect="All packages installed successfully",
                output="ERROR: Could not open requirements file: "
                       "[Errno 2] No such file or directory: 'requirements.txt'",
                user_message="install the dependencies",
            ),
            timeout=TIMEOUT,
        )
        assert validate_review(review) == []
        assert review["status"] == "replan"
        assert review["reason"]
        # Reason should be actionable
        assert len(review["reason"]) > 10


class TestPlanValidationRetry:
    async def test_retry_produces_valid_plan_after_feedback(
        self, live_config, seeded_db, live_session, tmp_path,
    ):
        """Patch validate_plan to reject the first call, verify retry works."""
        await save_message(seeded_db, live_session, "testadmin", "user", "hi")

        call_count = 0
        original_validate = validate_plan

        def rejecting_validate(plan, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ["Simulated validation error: please try again"]
            return original_validate(plan, **kwargs)

        with (
            patch("kiso.brain.KISO_DIR", tmp_path),
            patch("kiso.brain.discover_skills", return_value=[]),
            patch("kiso.brain.validate_plan", side_effect=rejecting_validate),
        ):
            plan = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin",
                    "What is the capital of Italy?",
                ),
                timeout=TIMEOUT * 2,
            )

        assert call_count >= 2
        # Final plan is valid (validated by the real function)
        assert original_validate(plan) == []
