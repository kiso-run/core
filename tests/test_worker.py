"""Tests for kiso/worker.py — per-session asyncio worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiso.brain import PlanError, ReviewError
from kiso.config import Config, Provider, KISO_DIR
from kiso.llm import LLMError
from kiso.store import (
    create_plan,
    create_session,
    create_task,
    get_plan_for_session,
    get_tasks_for_plan,
    get_tasks_for_session,
    get_unprocessed_messages,
    init_db,
    save_message,
)
from kiso.worker import (
    _exec_task, _msg_task, _load_worker_prompt, _session_workspace,
    _review_task, _execute_plan, _build_replan_context, _persist_plan_tasks,
    run_worker,
)


VALID_PLAN = {
    "goal": "Say hello",
    "secrets": None,
    "tasks": [{"type": "msg", "detail": "Hello!", "skill": None, "args": None, "expect": None}],
}

EXEC_THEN_MSG_PLAN = {
    "goal": "List files",
    "secrets": None,
    "tasks": [
        {"type": "exec", "detail": "echo hello", "skill": None, "args": None, "expect": "prints hello"},
        {"type": "msg", "detail": "Report the output", "skill": None, "args": None, "expect": None},
    ],
}

SKILL_PLAN = {
    "goal": "Use skill",
    "secrets": None,
    "tasks": [
        {"type": "skill", "detail": "search", "skill": "search", "args": "{}", "expect": "results"},
        {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
    ],
}

REVIEW_OK = {"status": "ok", "reason": None, "learn": None}
REVIEW_REPLAN = {"status": "replan", "reason": "Task failed", "learn": None}


def _make_config(**overrides) -> Config:
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"local": Provider(base_url="http://localhost:11434/v1")},
        users={},
        models={"planner": "gpt-4", "worker": "gpt-3.5", "reviewer": "gpt-4"},
        settings={
            "worker_idle_timeout": 1,  # short for tests
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
        },
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


# --- _exec_task ---

class TestExecTask:
    async def test_successful_command(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "echo hello", 5)
        assert stdout.strip() == "hello"
        assert success is True

    async def test_failing_command(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "ls /nonexistent_dir_xyz", 5)
        assert success is False
        assert stderr  # should have error message

    async def test_timeout(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "sleep 10", 1)
        assert success is False
        assert "Timed out" in stderr

    async def test_workspace_created(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            await _exec_task("new-sess", "echo ok", 5)
        assert (tmp_path / "sessions" / "new-sess").is_dir()

    async def test_captures_stderr(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "echo err >&2", 5)
        assert "err" in stderr

    async def test_runs_in_workspace_dir(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "pwd", 5)
        expected = str(tmp_path / "sessions" / "test-sess")
        assert stdout.strip() == expected


# --- _msg_task ---

class TestMsgTask:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_successful_call(self, db):
        config = _make_config()
        with patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="Bot says hi"):
            result = await _msg_task(config, db, "sess1", "Say hello to the user")
        assert result == "Bot says hi"

    async def test_includes_summary_in_context(self, db):
        config = _make_config()
        await db.execute("UPDATE sessions SET summary = 'Project uses Flask' WHERE session = 'sess1'")
        await db.commit()

        captured_messages = []

        async def _capture(cfg, role, messages):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.worker.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "task detail")

        user_content = captured_messages[1]["content"]
        assert "Project uses Flask" in user_content

    async def test_includes_facts_in_context(self, db):
        config = _make_config()
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Uses Python", "curator"))
        await db.commit()

        captured_messages = []

        async def _capture(cfg, role, messages):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.worker.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "task detail")

        user_content = captured_messages[1]["content"]
        assert "Uses Python" in user_content

    async def test_llm_error_propagates(self, db):
        config = _make_config()
        with patch("kiso.worker.call_llm", new_callable=AsyncMock, side_effect=LLMError("API down")):
            with pytest.raises(LLMError, match="API down"):
                await _msg_task(config, db, "sess1", "task")


# --- _load_worker_prompt ---

class TestLoadWorkerPrompt:
    def test_default_when_no_file(self):
        with patch.object(type(KISO_DIR / "roles" / "worker.md"), "exists", return_value=False):
            prompt = _load_worker_prompt()
        assert "helpful assistant" in prompt

    def test_reads_file_when_exists(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "worker.md").write_text("Custom worker prompt")
        with patch("kiso.worker.KISO_DIR", tmp_path):
            prompt = _load_worker_prompt()
        assert prompt == "Custom worker prompt"


# --- run_worker ---

class TestRunWorker:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_msg_only_plan_succeeds(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="Hi there!"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # Plan should be done
        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "done"

        # Task should be done with output
        tasks = await get_tasks_for_plan(db, plan["id"])
        assert len(tasks) == 1
        assert tasks[0]["status"] == "done"
        assert tasks[0]["output"] == "Hi there!"

    async def test_exec_then_msg_plan_succeeds(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "list files", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "list files", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=EXEC_THEN_MSG_PLAN), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="Here are the files"), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"

        tasks = await get_tasks_for_plan(db, plan["id"])
        assert len(tasks) == 2
        assert tasks[0]["type"] == "exec"
        assert tasks[0]["status"] == "done"
        assert "hello" in tasks[0]["output"]
        assert tasks[1]["type"] == "msg"
        assert tasks[1]["status"] == "done"

    async def test_exec_failure_review_replan(self, db, tmp_path):
        """Exec fails, reviewer says replan, but max_replan_depth=0 → immediate failure."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 0,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "fail", processed=False)

        fail_plan = {
            "goal": "Fail",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "fail", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

    async def test_exec_failure_review_error_marks_failed(self, db, tmp_path):
        """Exec fails, reviewer errors → plan failed without replan."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "fail", processed=False)

        fail_plan = {
            "goal": "Fail",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "fail", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("LLM down")), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

        tasks = await get_tasks_for_plan(db, plan["id"])
        assert tasks[0]["status"] == "failed"
        # Second task remains pending (never reached)
        assert tasks[1]["status"] == "pending"

    async def test_msg_llm_error_marks_plan_failed(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, side_effect=LLMError("API down")), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

    async def test_skill_task_fails_not_implemented(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Should eventually fail (after replan attempts)
        tasks = await get_tasks_for_session(db, "sess1")
        skill_tasks = [t for t in tasks if t["type"] == "skill"]
        assert all(t["status"] == "failed" for t in skill_tasks)
        assert any("not yet implemented" in (t["output"] or "") for t in skill_tasks)

    async def test_planning_error_continues(self, db, tmp_path):
        """If planning fails, the worker should continue to the next message."""
        config = _make_config()
        await create_session(db, "sess1")
        msg1 = await save_message(db, "sess1", "alice", "user", "bad", processed=False)
        msg2 = await save_message(db, "sess1", "alice", "user", "good", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg1, "content": "bad", "user_role": "admin"})
        await queue.put({"id": msg2, "content": "good", "user_role": "admin"})

        call_count = 0

        async def _planner_side_effect(db, config, session, role, content):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise PlanError("Planning failed")
            return VALID_PLAN

        with patch("kiso.worker.run_planner", side_effect=_planner_side_effect), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Both messages should be processed
        unprocessed = await get_unprocessed_messages(db)
        assert len(unprocessed) == 0

        # One plan should exist (from the second message)
        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "done"

    async def test_marks_message_processed(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        unprocessed = await get_unprocessed_messages(db)
        assert len(unprocessed) == 0

    async def test_idle_timeout_exits(self, db, tmp_path):
        """Worker exits after idle_timeout with no messages."""
        config = _make_config(settings={
            "worker_idle_timeout": 0.1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
        })
        await create_session(db, "sess1")
        queue: asyncio.Queue = asyncio.Queue()

        with patch("kiso.worker.KISO_DIR", tmp_path):
            # Should exit within ~0.2 seconds
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=2)
        # If we get here, the worker exited cleanly

    async def test_multiple_messages_processed_sequentially(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg1 = await save_message(db, "sess1", "alice", "user", "first", processed=False)
        msg2 = await save_message(db, "sess1", "alice", "user", "second", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg1, "content": "first", "user_role": "admin"})
        await queue.put({"id": msg2, "content": "second", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Both processed
        unprocessed = await get_unprocessed_messages(db)
        assert len(unprocessed) == 0

        # Two plans should exist
        tasks = await get_tasks_for_session(db, "sess1")
        assert len(tasks) == 2  # one msg task per plan

    async def test_replan_succeeds_on_second_attempt(self, db, tmp_path):
        """Exec fails, reviewer requests replan, second plan succeeds."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        fail_plan = {
            "goal": "First attempt",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        success_plan = {
            "goal": "Second attempt",
            "secrets": None,
            "tasks": [
                {"type": "msg", "detail": "Explaining the failure", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content):
            planner_calls.append(content)
            if len(planner_calls) == 1:
                return fail_plan
            return success_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.run_planner", side_effect=_planner), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="Fixed it"), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Latest plan should be done
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"
        assert plan["parent_id"] is not None  # links to first plan

        # Replan context was passed to planner
        assert len(planner_calls) == 2
        assert "Failure Reason" in planner_calls[1]

    async def test_replan_stores_learning(self, db, tmp_path):
        """Reviewer learning is stored in the learnings table."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        exec_plan = {
            "goal": "Run something",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo test", "skill": None, "args": None, "expect": "output"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        review_with_learning = {"status": "ok", "reason": None, "learn": "Project uses pytest"}

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=exec_plan), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=review_with_learning), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Check learning was saved
        cur = await db.execute("SELECT content FROM learnings WHERE session = 'sess1'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Project uses pytest"

    async def test_max_replan_depth_notifies_user(self, db, tmp_path):
        """When max replan depth is reached, a system message is saved."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 1,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "fail", processed=False)

        fail_plan = {
            "goal": "Will fail",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "fail", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        # System message about max replan depth
        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        rows = await cur.fetchall()
        system_msgs = [r[0] for r in rows]
        assert any("Max replan depth" in m for m in system_msgs)

    async def test_replan_error_breaks_loop(self, db, tmp_path):
        """If replanning raises PlanError, worker breaks and moves on."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "fail", processed=False)

        fail_plan = {
            "goal": "Will fail",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content):
            planner_calls.append(content)
            if len(planner_calls) == 1:
                return fail_plan
            raise PlanError("Replan LLM failed")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "fail", "user_role": "admin"})

        with patch("kiso.worker.run_planner", side_effect=_planner), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # First plan should be failed
        plans = []
        cur = await db.execute("SELECT * FROM plans WHERE session = 'sess1' ORDER BY id")
        plans = [dict(r) for r in await cur.fetchall()]
        assert len(plans) == 1
        assert plans[0]["status"] == "failed"

    async def test_replan_marks_remaining_tasks_superseded(self, db, tmp_path):
        """On replan, remaining pending tasks from old plan are marked failed."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        fail_plan = {
            "goal": "First",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "report", "skill": None, "args": None, "expect": None},
            ],
        }
        success_plan = {
            "goal": "Second",
            "secrets": None,
            "tasks": [
                {"type": "msg", "detail": "explain", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content):
            planner_calls.append(1)
            if len(planner_calls) == 1:
                return fail_plan
            return success_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.run_planner", side_effect=_planner), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Get first plan's tasks
        cur = await db.execute("SELECT * FROM plans WHERE session = 'sess1' ORDER BY id")
        plans = [dict(r) for r in await cur.fetchall()]
        assert len(plans) == 2

        first_tasks = await get_tasks_for_plan(db, plans[0]["id"])
        # The msg task was pending and should be marked failed (superseded)
        msg_task = [t for t in first_tasks if t["type"] == "msg"][0]
        assert msg_task["status"] == "failed"
        assert "Superseded" in msg_task["output"]

    async def test_replan_notification_saved(self, db, tmp_path):
        """On replan, a system message notifying the user is saved."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        fail_plan = {
            "goal": "Will fail",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }
        success_plan = {
            "goal": "Fixed",
            "secrets": None,
            "tasks": [
                {"type": "msg", "detail": "explain", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content):
            planner_calls.append(1)
            if len(planner_calls) == 1:
                return fail_plan
            return success_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.run_planner", side_effect=_planner), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        system_msgs = [r[0] for r in await cur.fetchall()]
        assert any("Replanning" in m for m in system_msgs)
        assert any("Task failed" in m for m in system_msgs)

    async def test_skill_review_error_fails_without_replan(self, db, tmp_path):
        """Skill task review error → plan fails without replan."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("LLM down")), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

    async def test_skill_review_ok_still_fails_plan(self, db, tmp_path):
        """Even if reviewer says ok for a skill task, plan still fails (skill not implemented)."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"


# --- _review_task ---

class TestReviewTask:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_ok_review(self, db):
        config = _make_config()
        task_row = {"detail": "echo hi", "expect": "prints hi", "output": "hi\n", "stderr": ""}
        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK):
            review = await _review_task(config, db, "sess1", "goal", task_row, "user msg")
        assert review["status"] == "ok"

    async def test_replan_review(self, db):
        config = _make_config()
        task_row = {"detail": "ls", "expect": "files", "output": "", "stderr": "not found"}
        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN):
            review = await _review_task(config, db, "sess1", "goal", task_row, "msg")
        assert review["status"] == "replan"

    async def test_stores_learning(self, db):
        config = _make_config()
        review_with_learn = {"status": "ok", "reason": None, "learn": "Uses Flask"}
        task_row = {"detail": "echo", "expect": "ok", "output": "ok", "stderr": ""}
        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=review_with_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT content FROM learnings WHERE session = 'sess1'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Uses Flask"

    async def test_no_learning_when_null(self, db):
        config = _make_config()
        task_row = {"detail": "echo", "expect": "ok", "output": "ok", "stderr": ""}
        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT COUNT(*) FROM learnings")
        count = (await cur.fetchone())[0]
        assert count == 0

    async def test_includes_stderr_in_output(self, db):
        config = _make_config()
        task_row = {"detail": "ls", "expect": "files", "output": "out", "stderr": "warn"}
        captured_output = []

        async def _mock_reviewer(cfg, goal, detail, expect, output, user_message):
            captured_output.append(output)
            return REVIEW_OK

        with patch("kiso.worker.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        assert "--- stderr ---" in captured_output[0]
        assert "warn" in captured_output[0]

    async def test_no_stderr_section_when_empty(self, db):
        config = _make_config()
        task_row = {"detail": "echo", "expect": "ok", "output": "ok", "stderr": ""}
        captured_output = []

        async def _mock_reviewer(cfg, goal, detail, expect, output, user_message):
            captured_output.append(output)
            return REVIEW_OK

        with patch("kiso.worker.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        assert "stderr" not in captured_output[0]

    async def test_none_output_handled(self, db):
        config = _make_config()
        task_row = {"detail": "echo", "expect": "ok", "output": None, "stderr": None}
        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK):
            review = await _review_task(config, db, "sess1", "goal", task_row, "msg")
        assert review["status"] == "ok"


# --- _build_replan_context ---

class TestBuildReplanContext:
    def test_minimal(self):
        ctx = _build_replan_context([], [], "Something broke", [])
        assert "## Failure Reason" in ctx
        assert "Something broke" in ctx
        assert "## Completed Tasks" not in ctx
        assert "## Remaining Tasks" not in ctx
        assert "## Previous Replan Attempts" not in ctx

    def test_with_completed(self):
        completed = [{"type": "exec", "detail": "echo hi", "status": "done", "output": "hi\n"}]
        ctx = _build_replan_context(completed, [], "broke", [])
        assert "## Completed Tasks" in ctx
        assert "[exec] echo hi: done" in ctx
        assert "hi" in ctx

    def test_with_remaining(self):
        remaining = [{"type": "msg", "detail": "report"}]
        ctx = _build_replan_context([], remaining, "broke", [])
        assert "## Remaining Tasks (not executed)" in ctx
        assert "[msg] report" in ctx

    def test_with_replan_history(self):
        history = [
            {"goal": "First try", "failure": "File not found"},
            {"goal": "Second try", "failure": "Permission denied"},
        ]
        ctx = _build_replan_context([], [], "Third failure", history)
        assert "## Previous Replan Attempts" in ctx
        assert "DO NOT repeat" in ctx
        assert "First try" in ctx
        assert "File not found" in ctx
        assert "Second try" in ctx
        assert "Permission denied" in ctx

    def test_output_truncated_to_500(self):
        long_output = "x" * 1000
        completed = [{"type": "exec", "detail": "cmd", "status": "done", "output": long_output}]
        ctx = _build_replan_context(completed, [], "broke", [])
        # Output should be truncated to 500 chars
        assert len(ctx) < 1000 + 200  # some overhead for labels

    def test_all_sections(self):
        completed = [{"type": "exec", "detail": "ls", "status": "done", "output": "files"}]
        remaining = [{"type": "msg", "detail": "tell user"}]
        history = [{"goal": "old", "failure": "old reason"}]
        ctx = _build_replan_context(completed, remaining, "new failure", history)
        assert "## Completed Tasks" in ctx
        assert "## Remaining Tasks" in ctx
        assert "## Failure Reason" in ctx
        assert "## Previous Replan Attempts" in ctx


# --- _execute_plan ---

class TestExecutePlan:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_msg_only_success(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        assert reason is None
        assert len(completed) == 1
        assert remaining == []

    async def test_exec_reviewed_ok(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(completed) == 2

    async def test_exec_reviewed_replan(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="exit 1", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        assert len(completed) == 0
        assert len(remaining) == 1  # msg task

    async def test_exec_review_error(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("down")), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None  # no replan, just failure
        assert len(remaining) == 1

    async def test_msg_llm_error(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.call_llm", new_callable=AsyncMock, side_effect=LLMError("down")), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None
        assert len(completed) == 0

    async def test_skill_reviewed_replan(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"

    async def test_skill_reviewed_ok_still_fails(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None  # failed but no replan

    async def test_skill_review_error(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("err")), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None

    async def test_multiple_exec_first_fails(self, db, tmp_path):
        """First exec fails → remaining tasks returned."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo first", expect="ok")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo second", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        review_calls = []

        async def _review_side_effect(cfg, goal, detail, expect, output, user_message):
            review_calls.append(detail)
            if "first" in detail:
                return REVIEW_REPLAN
            return REVIEW_OK

        with patch("kiso.worker.run_reviewer", side_effect=_review_side_effect), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert len(remaining) == 2  # second exec + msg
        assert len(review_calls) == 1  # only first exec reviewed


# --- _persist_plan_tasks ---

class TestPersistPlanTasks:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_persists_all_tasks(self, db):
        plan_id = await create_plan(db, "sess1", 1, "Test")
        tasks = [
            {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
            {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
        ]
        ids = await _persist_plan_tasks(db, plan_id, "sess1", tasks)
        assert len(ids) == 2

        db_tasks = await get_tasks_for_plan(db, plan_id)
        assert len(db_tasks) == 2
        assert db_tasks[0]["type"] == "exec"
        assert db_tasks[1]["type"] == "msg"

    async def test_persists_skill_fields(self, db):
        plan_id = await create_plan(db, "sess1", 1, "Test")
        tasks = [
            {"type": "skill", "detail": "search", "skill": "search", "args": '{"q":"test"}', "expect": "results"},
        ]
        ids = await _persist_plan_tasks(db, plan_id, "sess1", tasks)
        assert len(ids) == 1

        db_tasks = await get_tasks_for_plan(db, plan_id)
        assert db_tasks[0]["skill"] == "search"
        assert db_tasks[0]["args"] == '{"q":"test"}'


# --- store: save_learning ---

class TestSaveLearning:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_save_and_retrieve(self, db):
        from kiso.store import save_learning
        lid = await save_learning(db, "Uses Flask", "sess1", user="alice")
        assert isinstance(lid, int)
        assert lid > 0

        cur = await db.execute("SELECT * FROM learnings WHERE id = ?", (lid,))
        row = dict(await cur.fetchone())
        assert row["content"] == "Uses Flask"
        assert row["session"] == "sess1"
        assert row["user"] == "alice"
        assert row["status"] == "pending"

    async def test_save_without_user(self, db):
        from kiso.store import save_learning
        lid = await save_learning(db, "A fact", "sess1")
        cur = await db.execute("SELECT user FROM learnings WHERE id = ?", (lid,))
        row = await cur.fetchone()
        assert row[0] is None

    async def test_multiple_learnings(self, db):
        from kiso.store import save_learning
        await save_learning(db, "Fact 1", "sess1")
        await save_learning(db, "Fact 2", "sess1")
        cur = await db.execute("SELECT COUNT(*) FROM learnings WHERE session = 'sess1'")
        count = (await cur.fetchone())[0]
        assert count == 2
