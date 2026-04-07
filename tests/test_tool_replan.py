"""End-to-end smoke test for tool args correction via replan.

Simulates the real failure trace: browser tool is installed, planner sends
args: null, system detects the error and replans with corrected args.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiso.brain import PlanError, ReviewError, validate_plan
from kiso.store import (
    create_plan,
    create_session,
    create_task,
    get_plan_for_session,
    get_tasks_for_plan,
    get_tasks_for_session,
    init_db,
    save_message,
)
from kiso.worker import run_worker
from kiso.worker.loop import _execute_plan
from kiso.worker.loop import (
    _PlanCtx,
    _TaskHandlerResult,
    _handle_tool_task,
    _make_plan_output,
    _persist_plan_tasks,
    _run_planning_loop,
)

from tests.conftest import make_config, patch_kiso_dir as _patch_kiso_dir


BROWSER_TOOL_INFO = {
    "name": "browser",
    "summary": "Browser automation",
    "args_schema": {"action": {"type": "string", "required": True}},
    "entry": "browser.sh",
}

REVIEW_OK = {"status": "ok", "reason": None, "learn": None, "retry_hint": None, "summary": None}


class TestToolArgsReplanFlow:
    """End-to-end test: planner sends null args → validation catches it → replan with fixed args."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def test_validate_plan_catches_null_args(self):
        """validate_plan catches null args against schema."""
        plan = {
            "tasks": [
                {"type": "tool", "detail": "take screenshot", "tool": "browser",
                 "args": None, "expect": "screenshot saved"},
                {"type": "msg", "detail": "Answer in English. report results", "expect": None, "tool": None, "args": None},
            ],
        }
        info = {"browser": BROWSER_TOOL_INFO}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert any("missing required arg: action" in e for e in errors)

    async def test_tool_null_args_triggers_replan_then_succeeds(self, db, tmp_path):
        """Full flow: null args → setup fail → replan → corrected args → success."""
        config = make_config()

        # Initial plan: browser tool with null args (bad)
        bad_plan = {
            "goal": "Take screenshot",
            "secrets": None,
            "tasks": [
                {"type": "tool", "detail": "take screenshot of example.com",
                 "tool": "browser", "args": None, "expect": "screenshot saved"},
                {"type": "msg", "detail": "Report result", "tool": None,
                 "args": None, "expect": None},
            ],
        }

        # Corrected plan: browser tool with proper args
        good_plan = {
            "goal": "Take screenshot",
            "secrets": None,
            "tasks": [
                {"type": "tool", "detail": "take screenshot of example.com",
                 "tool": "browser",
                 "args": '{"action": "screenshot"}',
                 "expect": "screenshot saved"},
                {"type": "msg", "detail": "Report result", "tool": None,
                 "args": None, "expect": None},
            ],
        }

        plan_id = await create_plan(db, "sess1", 0, bad_plan["goal"])
        await _persist_plan_tasks(db, plan_id, "sess1", bad_plan["tasks"])

        call_count = [0]

        async def _mock_planner(db, config, session, role, content, **kwargs):
            call_count[0] += 1
            return good_plan

        with patch("kiso.worker.loop.run_planner", side_effect=_mock_planner), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop._tool_task", new_callable=AsyncMock,
                   return_value=("screenshot saved to file.png", "", True, 0)), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Screenshot taken!"), \
             patch("kiso.worker.loop.discover_tools", return_value=[BROWSER_TOOL_INFO]), \
             _patch_kiso_dir(tmp_path):
            returned_id = await _run_planning_loop(
                db, config, "sess1", 0, "take screenshot of example.com",
                plan_id, bad_plan, "admin", None, 30,
                {}, None, 2, None, None,
            )

        # Should have replanned once (bad plan → replan → good plan)
        assert returned_id != plan_id  # new plan was created
        assert call_count[0] == 1  # planner called once for replan

        # Verify the original plan failed and the replan succeeded
        plans = await db.execute_fetchall(
            "SELECT * FROM plans WHERE session = 'sess1' ORDER BY id"
        )
        assert len(plans) == 2
        assert plans[0]["status"] == "failed"  # original with bad args
        assert plans[1]["status"] == "done"  # replan with corrected args

    async def test_tool_setup_error_provides_context_for_replan(self, db, tmp_path):
        """The replan context includes the tool setup error for the planner to fix."""
        config = make_config()
        tool_info_with_schema = {
            "name": "browser",
            "args_schema": {"action": {"type": "string", "required": True},
                            "url": {"type": "string", "required": False}},
            "entry": "browser.sh",
        }
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="tool",
                          detail="take screenshot", skill="browser",
                          args=None, expect="screenshot")

        ctx = _PlanCtx(
            db=db, config=config, session="sess1",
            goal="Test", user_message="msg",
            deploy_secrets={}, session_secrets={},
            max_output_size=4096, max_worker_retries=1,
            messenger_timeout=5, installed_tools=[tool_info_with_schema],
            slog=None, sandbox_uid=None,
        )
        tasks = await get_tasks_for_plan(db, plan_id)
        result = await _handle_tool_task(ctx, tasks[0], 0, False, 0)

        assert result.stop_replan is not None
        assert "missing required arg: action" in result.stop_replan
        assert result.plan_output is not None
        assert "missing required arg" in result.plan_output["output"]
