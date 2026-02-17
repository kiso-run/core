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
    _exec_task, _msg_task, _skill_task, _load_worker_prompt, _session_workspace,
    _review_task, _execute_plan, _build_replan_context, _persist_plan_tasks,
    _write_plan_outputs, _cleanup_plan_outputs, _format_plan_outputs_for_msg,
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

    async def test_plan_with_secrets_extracts_them(self, db, tmp_path):
        """Secrets from the plan are extracted and available for skill execution."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "use token", processed=False)

        plan_with_secrets = {
            "goal": "Use token",
            "secrets": [{"key": "api_token", "value": "tok_abc"}],
            "tasks": [{"type": "msg", "detail": "Done", "skill": None, "args": None, "expect": None}],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "use token", "user_role": "admin"})

        with patch("kiso.worker.run_planner", new_callable=AsyncMock, return_value=plan_with_secrets), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"

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
        assert any("not installed" in (t["output"] or "") for t in skill_tasks)

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

        async def _planner_side_effect(db, config, session, role, content, **kwargs):
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

        async def _planner(db, config, session, role, content, **kwargs):
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

        async def _planner(db, config, session, role, content, **kwargs):
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

        # User should be notified that replan failed
        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        system_msgs = [r[0] for r in await cur.fetchall()]
        assert any("Replan failed" in m for m in system_msgs)

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

        async def _planner(db, config, session, role, content, **kwargs):
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

        async def _planner(db, config, session, role, content, **kwargs):
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
            {"goal": "First try", "failure": "File not found", "what_was_tried": ["[exec] ls /bad"]},
            {"goal": "Second try", "failure": "Permission denied", "what_was_tried": ["[exec] cat /etc/shadow"]},
        ]
        ctx = _build_replan_context([], [], "Third failure", history)
        assert "## Previous Replan Attempts" in ctx
        assert "DO NOT repeat" in ctx
        assert "First try" in ctx
        assert "File not found" in ctx
        assert "[exec] ls /bad" in ctx
        assert "Second try" in ctx
        assert "Permission denied" in ctx
        assert "[exec] cat /etc/shadow" in ctx

    def test_output_truncated_to_500(self):
        long_output = "x" * 1000
        completed = [{"type": "exec", "detail": "cmd", "status": "done", "output": long_output}]
        ctx = _build_replan_context(completed, [], "broke", [])
        # The 1000-char output should be truncated to exactly 500 chars
        assert "x" * 500 in ctx
        assert "x" * 501 not in ctx

    def test_history_without_what_was_tried(self):
        """Handles legacy history entries without what_was_tried."""
        history = [{"goal": "old", "failure": "reason"}]
        ctx = _build_replan_context([], [], "fail", history)
        assert "Tried: nothing" in ctx

    def test_all_sections(self):
        completed = [{"type": "exec", "detail": "ls", "status": "done", "output": "files"}]
        remaining = [{"type": "msg", "detail": "tell user"}]
        history = [{"goal": "old", "failure": "old reason", "what_was_tried": ["[exec] ls"]}]
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


# --- M6: Task output chaining ---

# --- _write_plan_outputs ---

class TestWritePlanOutputs:
    def test_writes_json_file(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            outputs = [
                {"index": 1, "type": "exec", "detail": "echo hi", "output": "hi\n", "status": "done"},
            ]
            _write_plan_outputs("sess1", outputs)

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["output"] == "hi\n"

    def test_overwrites_previous(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            _write_plan_outputs("sess1", [{"index": 1, "type": "exec", "detail": "a", "output": "1", "status": "done"}])
            _write_plan_outputs("sess1", [
                {"index": 1, "type": "exec", "detail": "a", "output": "1", "status": "done"},
                {"index": 2, "type": "exec", "detail": "b", "output": "2", "status": "done"},
            ])

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        data = json.loads(path.read_text())
        assert len(data) == 2

    def test_empty_outputs(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            _write_plan_outputs("sess1", [])

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert path.exists()
        assert json.loads(path.read_text()) == []


# --- _cleanup_plan_outputs ---

class TestCleanupPlanOutputs:
    def test_removes_file(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            _write_plan_outputs("sess1", [{"index": 1, "type": "exec", "detail": "a", "output": "x", "status": "done"}])
            path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
            assert path.exists()
            _cleanup_plan_outputs("sess1")
            assert not path.exists()

    def test_no_error_if_missing(self, tmp_path):
        with patch("kiso.worker.KISO_DIR", tmp_path):
            # Ensure workspace exists but no file
            _session_workspace("sess1")
            _cleanup_plan_outputs("sess1")  # should not raise


# --- _format_plan_outputs_for_msg ---

class TestFormatPlanOutputsForMsg:
    def test_empty_returns_empty_string(self):
        assert _format_plan_outputs_for_msg([]) == ""

    def test_single_entry(self):
        outputs = [{"index": 1, "type": "exec", "detail": "echo hi", "output": "hi\n", "status": "done"}]
        result = _format_plan_outputs_for_msg(outputs)
        assert "[1] exec: echo hi" in result
        assert "Status: done" in result
        assert "hi\n" in result

    def test_multiple_entries(self):
        outputs = [
            {"index": 1, "type": "exec", "detail": "echo a", "output": "a", "status": "done"},
            {"index": 2, "type": "msg", "detail": "report", "output": "report text", "status": "done"},
        ]
        result = _format_plan_outputs_for_msg(outputs)
        assert "[1] exec: echo a" in result
        assert "[2] msg: report" in result

    def test_no_output_placeholder(self):
        outputs = [{"index": 1, "type": "exec", "detail": "cmd", "output": None, "status": "failed"}]
        result = _format_plan_outputs_for_msg(outputs)
        assert "(no output)" in result

    def test_fenced_in_backticks(self):
        outputs = [{"index": 1, "type": "exec", "detail": "cmd", "output": "data", "status": "done"}]
        result = _format_plan_outputs_for_msg(outputs)
        assert "```" in result


# --- _msg_task with plan_outputs ---

class TestMsgTaskWithPlanOutputs:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_includes_plan_outputs_in_context(self, db):
        config = _make_config()
        plan_outputs = [
            {"index": 1, "type": "exec", "detail": "echo hi", "output": "hi\n", "status": "done"},
        ]
        captured_messages = []

        async def _capture(cfg, role, messages):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.worker.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Report results", plan_outputs=plan_outputs)

        user_content = captured_messages[1]["content"]
        assert "## Preceding Task Outputs" in user_content
        assert "[1] exec: echo hi" in user_content

    async def test_no_plan_outputs_section_when_none(self, db):
        config = _make_config()
        captured_messages = []

        async def _capture(cfg, role, messages):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.worker.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Report results")

        user_content = captured_messages[1]["content"]
        assert "Preceding Task Outputs" not in user_content

    async def test_no_plan_outputs_section_when_empty(self, db):
        config = _make_config()
        captured_messages = []

        async def _capture(cfg, role, messages):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.worker.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Report results", plan_outputs=[])

        user_content = captured_messages[1]["content"]
        assert "Preceding Task Outputs" not in user_content


# --- _execute_plan: plan_outputs chaining ---

class TestExecutePlanOutputChaining:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_plan_outputs_json_written_before_exec(self, db, tmp_path):
        """plan_outputs.json should exist in workspace before exec runs."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo first", expect="ok")
        await create_task(db, plan_id, "sess1", type="exec", detail="cat .kiso/plan_outputs.json", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # Second exec read plan_outputs.json which should contain first exec output
        second_exec = completed[1]
        data = json.loads(second_exec["output"])
        assert len(data) == 1
        assert data[0]["detail"] == "echo first"
        assert "first" in data[0]["output"]

    async def test_plan_outputs_cleaned_up_on_success(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        outputs_file = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert not outputs_file.exists()

    async def test_plan_outputs_cleaned_up_on_replan(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="exit 1", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        outputs_file = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert not outputs_file.exists()

    async def test_plan_outputs_cleaned_up_on_llm_error(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.call_llm", new_callable=AsyncMock, side_effect=LLMError("down")), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        outputs_file = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert not outputs_file.exists()

    async def test_msg_receives_exec_outputs(self, db, tmp_path):
        """msg task should receive preceding exec outputs via plan_outputs."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo hello", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="Report what happened")

        captured_messages = []

        async def _capture_llm(cfg, role, messages):
            captured_messages.extend(messages)
            return "Report done"

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.call_llm", side_effect=_capture_llm), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # The msg task's LLM call should include preceding outputs
        user_content = captured_messages[1]["content"]
        assert "## Preceding Task Outputs" in user_content
        assert "echo hello" in user_content
        assert "hello" in user_content

    async def test_plan_outputs_accumulates_all_types(self, db, tmp_path):
        """plan_outputs should accumulate entries for exec, msg, and skill tasks."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo step1", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="Report step1")

        msg_calls = []

        async def _capture_llm(cfg, role, messages):
            msg_calls.append(messages)
            return "Step 1 report"

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.call_llm", side_effect=_capture_llm), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(completed) == 2
        # msg task had 1 preceding exec output
        assert "## Preceding Task Outputs" in msg_calls[0][1]["content"]

    async def test_skill_output_in_plan_outputs(self, db, tmp_path):
        """Skill task output should be accumulated in plan_outputs."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        # Skill accumulated in plan_outputs before cleanup — verify via DB task output
        tasks = await get_tasks_for_plan(db, plan_id)
        assert "not installed" in tasks[0]["output"]


# --- M7: _skill_task ---

def _create_echo_skill(tmp_path: Path) -> dict:
    """Create a minimal echo skill for testing and return its info dict."""
    skill_dir = tmp_path / "skills" / "echo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "run.py").write_text(
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(json.dumps(data['args']))\n"
    )
    (skill_dir / "kiso.toml").write_text(
        '[kiso]\ntype = "skill"\nname = "echo"\n'
        '[kiso.skill]\nsummary = "Echo"\n'
        '[kiso.skill.args]\ntext = { type = "string", required = true }\n'
    )
    (skill_dir / "pyproject.toml").write_text('[project]\nname = "echo"\nversion = "0.1.0"')
    return {
        "name": "echo",
        "summary": "Echo",
        "args_schema": {"text": {"type": "string", "required": True}},
        "env": {},
        "session_secrets": [],
        "path": str(skill_dir),
        "version": "0.1.0",
        "description": "",
    }


class TestSkillTask:
    async def test_successful_skill(self, tmp_path):
        skill = _create_echo_skill(tmp_path)
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _skill_task(
                "sess1", skill, {"text": "hello"}, None, None, 5,
            )
        assert success is True
        result = json.loads(stdout)
        assert result["text"] == "hello"

    async def test_skill_receives_plan_outputs(self, tmp_path):
        # Create skill that dumps full input
        skill_dir = tmp_path / "skills" / "dump"
        skill_dir.mkdir(parents=True)
        (skill_dir / "run.py").write_text(
            "import json, sys\ndata = json.load(sys.stdin)\nprint(json.dumps(data))\n"
        )
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "dump"\n'
            '[kiso.skill]\nsummary = "Dump"\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "dump"\nversion = "0.1.0"')
        skill = {
            "name": "dump", "summary": "Dump", "args_schema": {}, "env": {},
            "session_secrets": [], "path": str(skill_dir), "version": "0.1.0", "description": "",
        }

        plan_outputs = [{"index": 1, "type": "exec", "detail": "ls", "output": "files", "status": "done"}]
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, _, success = await _skill_task(
                "sess1", skill, {}, plan_outputs, None, 5,
            )
        assert success is True
        data = json.loads(stdout)
        assert len(data["plan_outputs"]) == 1
        assert data["plan_outputs"][0]["output"] == "files"

    async def test_skill_scoped_secrets(self, tmp_path):
        skill_dir = tmp_path / "skills" / "sec"
        skill_dir.mkdir(parents=True)
        (skill_dir / "run.py").write_text(
            "import json, sys\ndata = json.load(sys.stdin)\nprint(json.dumps(data['session_secrets']))\n"
        )
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "sec"\n'
            '[kiso.skill]\nsummary = "Sec"\nsession_secrets = ["api_token"]\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "sec"\nversion = "0.1.0"')
        skill = {
            "name": "sec", "summary": "Sec", "args_schema": {}, "env": {},
            "session_secrets": ["api_token"], "path": str(skill_dir),
            "version": "0.1.0", "description": "",
        }

        secrets = {"api_token": "tok_123", "other": "should_not_appear"}
        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, _, success = await _skill_task(
                "sess1", skill, {}, None, secrets, 5,
            )
        assert success is True
        result = json.loads(stdout)
        assert result == {"api_token": "tok_123"}

    async def test_skill_timeout(self, tmp_path):
        skill_dir = tmp_path / "skills" / "slow"
        skill_dir.mkdir(parents=True)
        (skill_dir / "run.py").write_text("import time; time.sleep(10)")
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "slow"\n'
            '[kiso.skill]\nsummary = "Slow"\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "slow"\nversion = "0.1.0"')
        skill = {
            "name": "slow", "summary": "Slow", "args_schema": {}, "env": {},
            "session_secrets": [], "path": str(skill_dir), "version": "0.1.0", "description": "",
        }

        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _skill_task(
                "sess1", skill, {}, None, None, 1,
            )
        assert success is False
        assert "Timed out" in stderr

    async def test_skill_failing_script(self, tmp_path):
        skill_dir = tmp_path / "skills" / "fail"
        skill_dir.mkdir(parents=True)
        (skill_dir / "run.py").write_text("import sys; print('err msg', file=sys.stderr); sys.exit(1)")
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "fail"\n'
            '[kiso.skill]\nsummary = "Fail"\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "fail"\nversion = "0.1.0"')
        skill = {
            "name": "fail", "summary": "Fail", "args_schema": {}, "env": {},
            "session_secrets": [], "path": str(skill_dir), "version": "0.1.0", "description": "",
        }

        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _skill_task(
                "sess1", skill, {}, None, None, 5,
            )
        assert success is False
        assert "err msg" in stderr

    async def test_skill_executable_not_found(self, tmp_path):
        skill_dir = tmp_path / "skills" / "broken"
        skill_dir.mkdir(parents=True)
        # Point run.py to a nonexistent path
        (skill_dir / "run.py").write_text("pass")
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "broken"\n'
            '[kiso.skill]\nsummary = "Broken"\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "broken"\nversion = "0.1.0"')
        # Create a venv with a python "file" that is actually a directory
        # This will trigger FileNotFoundError from create_subprocess_exec
        venv_python = skill_dir / ".venv" / "bin" / "python"
        venv_python.mkdir(parents=True)  # directory, not file
        skill = {
            "name": "broken", "summary": "Broken", "args_schema": {}, "env": {},
            "session_secrets": [], "path": str(skill_dir), "version": "0.1.0", "description": "",
        }

        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, stderr, success = await _skill_task(
                "sess1", skill, {}, None, None, 5,
            )
        assert success is False
        # Might be PermissionError or similar — just check it failed
        assert stderr != ""

    async def test_skill_runs_in_workspace(self, tmp_path):
        skill_dir = tmp_path / "skills" / "pwd"
        skill_dir.mkdir(parents=True)
        (skill_dir / "run.py").write_text("import os; print(os.getcwd())")
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "pwd"\n'
            '[kiso.skill]\nsummary = "Pwd"\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "pwd"\nversion = "0.1.0"')
        skill = {
            "name": "pwd", "summary": "Pwd", "args_schema": {}, "env": {},
            "session_secrets": [], "path": str(skill_dir), "version": "0.1.0", "description": "",
        }

        with patch("kiso.worker.KISO_DIR", tmp_path):
            stdout, _, success = await _skill_task(
                "sess1", skill, {}, None, None, 5,
            )
        assert success is True
        expected = str(tmp_path / "sessions" / "sess1")
        assert stdout.strip() == expected


# --- M7: _execute_plan with real skill ---

class TestExecutePlanSkill:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_skill_not_installed(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="nonexistent", args='{"q":"test"}', expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        tasks = await get_tasks_for_plan(db, plan_id)
        assert "not installed" in tasks[0]["output"]

    async def test_skill_invalid_args_json(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args="not json", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        # Create a real skill so lookup succeeds
        skill = _create_echo_skill(tmp_path)
        skills_dir = tmp_path / "skills"

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.discover_skills", return_value=[skill]), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        tasks = await get_tasks_for_plan(db, plan_id)
        assert "Invalid skill args JSON" in tasks[0]["output"]

    async def test_skill_args_validation_error(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        # Missing required arg 'text'
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.discover_skills", return_value=[skill]), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        tasks = await get_tasks_for_plan(db, plan_id)
        assert "validation failed" in tasks[0]["output"]
        assert "missing required arg: text" in tasks[0]["output"]

    async def test_skill_executes_successfully(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{"text":"hello"}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.discover_skills", return_value=[skill]), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(completed) == 2
        # First task is skill with output
        skill_task = completed[0]
        assert skill_task["status"] == "done"
        result = json.loads(skill_task["output"])
        assert result["text"] == "hello"

    async def test_skill_passes_session_secrets(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="sec",
                          skill="sec", args='{}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        # Create skill that outputs session_secrets
        skill_dir = tmp_path / "skills" / "sec"
        skill_dir.mkdir(parents=True)
        (skill_dir / "run.py").write_text(
            "import json, sys\ndata = json.load(sys.stdin)\nprint(json.dumps(data['session_secrets']))\n"
        )
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "sec"\n'
            '[kiso.skill]\nsummary = "Sec"\nsession_secrets = ["api_token"]\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "sec"\nversion = "0.1.0"')
        skill = {
            "name": "sec", "summary": "Sec", "args_schema": {},
            "env": {}, "session_secrets": ["api_token"],
            "path": str(skill_dir), "version": "0.1.0", "description": "",
        }

        secrets = {"api_token": "tok_xyz", "other": "hidden"}

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.call_llm", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.discover_skills", return_value=[skill]), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                session_secrets=secrets,
            )

        assert success is True
        result = json.loads(completed[0]["output"])
        assert result == {"api_token": "tok_xyz"}

    async def test_skill_review_replan(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{"text":"hi"}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        with patch("kiso.worker.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.discover_skills", return_value=[skill]), \
             patch("kiso.worker.KISO_DIR", tmp_path):
            success, reason, _, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        assert len(remaining) == 1  # msg task remaining
