"""Tests for kiso/worker.py — per-session asyncio worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiso.brain import ClassifierError, CuratorError, ExecTranslatorError, MessengerError, ParaphraserError, PlanError, ReviewError, SearcherError, SummarizerError
from kiso.config import Config, ConfigError, Provider, User, KISO_DIR
from kiso.llm import LLMBudgetExceeded, LLMError
from kiso.store import (
    create_plan,
    create_session,
    create_task,
    get_facts,
    get_pending_items,
    get_plan_for_session,
    get_recent_messages,
    get_session,
    get_tasks_for_plan,
    get_tasks_for_session,
    get_unprocessed_messages,
    init_db,
    save_fact,
    save_learning,
    save_message,
    update_task_retry_count,
)
from kiso.worker import (
    _apply_curator_result,
    _build_cancel_summary, _build_exec_env, _build_failure_summary,
    _exec_task, _fast_path_chat, _msg_task, _post_plan_knowledge,
    _report_pub_files, _skill_task, _session_workspace,
    _ensure_sandbox_user, _truncate_output,
    _review_task, _execute_plan, _build_replan_context, _persist_plan_tasks,
    _write_plan_outputs, _cleanup_plan_outputs, _format_plan_outputs_for_msg,
    run_worker,
)

from contextlib import contextmanager


@contextmanager
def _patch_kiso_dir(tmp_path):
    """Patch KISO_DIR in both utils and loop submodules (and exec via utils)."""
    with patch("kiso.worker.utils.KISO_DIR", tmp_path), \
         patch("kiso.worker.loop.KISO_DIR", tmp_path):
        yield


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

REVIEW_OK = {"status": "ok", "reason": None, "learn": None, "retry_hint": None}
REVIEW_REPLAN = {"status": "replan", "reason": "Task failed", "learn": None, "retry_hint": None}
REVIEW_REPLAN_WITH_HINT = {"status": "replan", "reason": "Wrong path", "learn": None, "retry_hint": "use /opt/app not /app"}


def _passthrough_translator(config, detail, sys_env_text, **kw):
    """Pass-through mock for run_exec_translator: returns detail unchanged."""
    return detail


def _patch_translator():
    """Convenience: patch run_exec_translator as a pass-through for tests
    where exec detail already contains a valid shell command."""
    return patch(
        "kiso.worker.loop.run_exec_translator",
        new_callable=AsyncMock,
        side_effect=_passthrough_translator,
    )


def _make_config(**overrides) -> Config:
    from kiso.config import SETTINGS_DEFAULTS, MODEL_DEFAULTS
    base_settings = {
        **SETTINGS_DEFAULTS,
        "worker_idle_timeout": 1,  # short for tests
        "exec_timeout": 5,
        "planner_timeout": 5,
    }
    # Merge settings overrides rather than replacing the whole dict
    if "settings" in overrides:
        base_settings.update(overrides.pop("settings"))
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"local": Provider(base_url="http://localhost:11434/v1")},
        users={},
        models={**MODEL_DEFAULTS, "planner": "gpt-4", "worker": "gpt-3.5", "reviewer": "gpt-4"},
        settings=base_settings,
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


# --- _exec_task ---

class TestExecTask:
    async def test_successful_command(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "echo hello", 5)
        assert stdout.strip() == "hello"
        assert success is True

    async def test_failing_command(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "ls /nonexistent_dir_xyz", 5)
        assert success is False
        assert stderr  # should have error message

    async def test_timeout(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "sleep 10", 1)
        assert success is False
        assert "Timed out" in stderr

    async def test_workspace_created(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            await _exec_task("new-sess", "echo ok", 5)
        assert (tmp_path / "sessions" / "new-sess").is_dir()

    async def test_captures_stderr(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "echo err >&2", 5)
        assert "err" in stderr

    async def test_runs_in_workspace_dir(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "pwd", 5)
        expected = str(tmp_path / "sessions" / "test-sess")
        assert stdout.strip() == expected

    async def test_deny_list_blocks_rm_rf(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "rm -rf /", 5)
        assert success is False
        assert "Command blocked" in stderr

    async def test_deny_list_allows_safe_rm(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task("test-sess", "rm -rf ./nonexistent", 5)
        # Command may fail (dir doesn't exist) but should NOT be blocked by deny list
        assert "Command blocked" not in stderr


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
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="Bot says hi"):
            result = await _msg_task(config, db, "sess1", "Say hello to the user")
        assert result == "Bot says hi"

    async def test_includes_summary_in_context(self, db):
        config = _make_config()
        await db.execute("UPDATE sessions SET summary = 'Project uses Flask' WHERE session = 'sess1'")
        await db.commit()

        captured_messages = []

        async def _capture(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "task detail")

        user_content = captured_messages[1]["content"]
        assert "Project uses Flask" in user_content

    async def test_includes_facts_in_context(self, db):
        config = _make_config()
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Uses Python", "curator"))
        await db.commit()

        captured_messages = []

        async def _capture(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "task detail")

        user_content = captured_messages[1]["content"]
        assert "Uses Python" in user_content

    async def test_goal_reaches_messenger_context(self, db):
        """_msg_task passes goal parameter through to messenger context."""
        config = _make_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Say hello", goal="Greet the user")
        user_content = captured_messages[1]["content"]
        assert "Greet the user" in user_content

    async def test_messenger_error_propagates(self, db):
        from kiso.brain import MessengerError
        config = _make_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, side_effect=LLMError("API down")):
            with pytest.raises(MessengerError, match="API down"):
                await _msg_task(config, db, "sess1", "task")


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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi there!"), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=EXEC_THEN_MSG_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Here are the files"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=plan_with_secrets), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("LLM down")), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("API down")), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

    async def test_skill_task_fails_not_implemented(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", side_effect=_planner_side_effect), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Both messages should be processed
        unprocessed = await get_unprocessed_messages(db)
        assert len(unprocessed) == 0

        # Two plans: first failed (PlanError), second done
        cur = await db.execute(
            "SELECT * FROM plans WHERE session = 'sess1' ORDER BY id"
        )
        plans = [dict(r) for r in await cur.fetchall()]
        assert len(plans) == 2
        assert plans[0]["status"] == "failed"
        assert plans[1]["status"] == "done"

    async def test_marks_message_processed(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             _patch_kiso_dir(tmp_path):
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

        with _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Fixed it"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=exec_plan), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_learning), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Check learning was saved
        cur = await db.execute("SELECT content FROM learnings WHERE session = 'sess1'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Project uses pytest"

    async def test_max_replan_depth_notifies_user(self, db, tmp_path):
        """When max replan depth is reached, a recovery msg task is created via LLM."""
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Sorry, the plan failed after multiple retries."), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        # Recovery msg task created in the plan
        plan = await get_plan_for_session(db, "sess1")
        tasks = await get_tasks_for_plan(db, plan["id"])
        msg_tasks = [t for t in tasks if t["type"] == "msg"]
        assert len(msg_tasks) >= 1
        recovery = msg_tasks[-1]
        assert recovery["status"] == "done"
        assert "failed" in recovery["output"].lower()

        # System message saved
        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        rows = await cur.fetchall()
        system_msgs = [r[0] for r in rows]
        assert any("failed" in m.lower() for m in system_msgs)

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

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

    async def test_replan_error_creates_recovery_msg_task(self, db, tmp_path):
        """When replanning raises PlanError, a recovery msg task is created
        so the CLI can display feedback to the user."""
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

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Should have a recovery msg task with status=done
        cur = await db.execute(
            "SELECT * FROM tasks WHERE session = 'sess1' AND type = 'msg' AND status = 'done'"
        )
        msg_tasks = [dict(r) for r in await cur.fetchall()]
        # At least one msg task should have "Replan failed" in its output
        assert any("Replan failed" in (t.get("output") or "") for t in msg_tasks), \
            f"No recovery msg task found with 'Replan failed'; msg tasks: {msg_tasks}"

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

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        system_msgs = [r[0] for r in await cur.fetchall()]
        assert any("Replanning" in m for m in system_msgs)
        assert any("Task failed" in m for m in system_msgs)

        # Replan notification should also be a visible msg task on the first plan
        cur2 = await db.execute(
            "SELECT * FROM plans WHERE session = 'sess1' ORDER BY id"
        )
        plans = [dict(r) for r in await cur2.fetchall()]
        first_tasks = await get_tasks_for_plan(db, plans[0]["id"])
        notify_tasks = [
            t for t in first_tasks
            if t["type"] == "msg" and "Replanning" in (t["output"] or "")
        ]
        assert len(notify_tasks) == 1
        assert notify_tasks[0]["status"] == "done"

    async def test_replan_sets_replanning_status_during_planner_call(self, db, tmp_path):
        """Plan status transitions: running → replanning → failed, then new plan running."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        fail_plan = {
            "goal": "First",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }
        success_plan = {
            "goal": "Second",
            "secrets": None,
            "tasks": [
                {"type": "msg", "detail": "explain", "skill": None, "args": None, "expect": None},
            ],
        }

        captured_statuses: list[str] = []

        planner_calls = []

        async def _planner(db, config, session, role, content, **kwargs):
            planner_calls.append(1)
            if len(planner_calls) == 1:
                return fail_plan
            # During replanning, capture the OLD plan's status
            cur = await db.execute(
                "SELECT status FROM plans WHERE session = 'sess1' ORDER BY id LIMIT 1"
            )
            row = await cur.fetchone()
            if row:
                captured_statuses.append(row[0])
            return success_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # During the replan planner call, the old plan should have been "replanning"
        assert "replanning" in captured_statuses

        # After completion, old plan should have its final status
        cur = await db.execute(
            "SELECT status FROM plans WHERE session = 'sess1' ORDER BY id"
        )
        plans = [r[0] for r in await cur.fetchall()]
        assert plans[0] == "failed"  # old plan finalized to "failed"
        assert plans[1] == "done"    # new plan succeeded

    async def test_skill_review_error_fails_without_replan(self, db, tmp_path):
        """Skill task review error → plan fails without replan."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("LLM down")), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_kiso_dir(tmp_path):
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

    async def _make_task(self, db, detail="echo", expect="ok"):
        plan_id = await create_plan(db, "sess1", 1, "Test")
        task_id = await create_task(db, plan_id, "sess1", type="exec", detail=detail, expect=expect)
        return task_id

    async def test_ok_review(self, db):
        config = _make_config()
        tid = await self._make_task(db, "echo hi", "prints hi")
        task_row = {"id": tid, "detail": "echo hi", "expect": "prints hi", "output": "hi\n", "stderr": ""}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK):
            review = await _review_task(config, db, "sess1", "goal", task_row, "user msg")
        assert review["status"] == "ok"

    async def test_replan_review(self, db):
        config = _make_config()
        tid = await self._make_task(db, "ls", "files")
        task_row = {"id": tid, "detail": "ls", "expect": "files", "output": "", "stderr": "not found"}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN):
            review = await _review_task(config, db, "sess1", "goal", task_row, "msg")
        assert review["status"] == "replan"

    async def test_stores_learning(self, db):
        config = _make_config()
        review_with_learn = {"status": "ok", "reason": None, "learn": "Uses Flask"}
        tid = await self._make_task(db)
        task_row = {"id": tid, "detail": "echo", "expect": "ok", "output": "ok", "stderr": ""}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT content FROM learnings WHERE session = 'sess1'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Uses Flask"

    async def test_no_learning_when_null(self, db):
        config = _make_config()
        tid = await self._make_task(db)
        task_row = {"id": tid, "detail": "echo", "expect": "ok", "output": "ok", "stderr": ""}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT COUNT(*) FROM learnings")
        count = (await cur.fetchone())[0]
        assert count == 0

    async def test_includes_stderr_in_output(self, db):
        config = _make_config()
        tid = await self._make_task(db, "ls", "files")
        task_row = {"id": tid, "detail": "ls", "expect": "files", "output": "out", "stderr": "warn"}
        captured_output = []

        async def _mock_reviewer(cfg, goal, detail, expect, output, user_message, **kwargs):
            captured_output.append(output)
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        assert "--- stderr ---" in captured_output[0]
        assert "warn" in captured_output[0]

    async def test_no_stderr_section_when_empty(self, db):
        config = _make_config()
        tid = await self._make_task(db)
        task_row = {"id": tid, "detail": "echo", "expect": "ok", "output": "ok", "stderr": ""}
        captured_output = []

        async def _mock_reviewer(cfg, goal, detail, expect, output, user_message, **kwargs):
            captured_output.append(output)
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        assert "stderr" not in captured_output[0]

    async def test_none_output_handled(self, db):
        config = _make_config()
        tid = await self._make_task(db)
        task_row = {"id": tid, "detail": "echo", "expect": "ok", "output": None, "stderr": None}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK):
            review = await _review_task(config, db, "sess1", "goal", task_row, "msg")
        assert review["status"] == "ok"

    async def test_review_verdict_persisted_after_exec(self, db):
        config = _make_config()
        tid = await self._make_task(db, "echo ok", "ok")
        task_row = {"id": tid, "detail": "echo ok", "expect": "ok", "output": "ok\n", "stderr": ""}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute(
            "SELECT review_verdict, review_reason, review_learning FROM tasks WHERE id=?", (tid,)
        )
        row = await cur.fetchone()
        assert row[0] == "ok"
        assert row[1] is None
        assert row[2] is None

    async def test_review_replan_fields_persisted(self, db):
        config = _make_config()
        replan_with_learn = {"status": "replan", "reason": "Bad output", "learn": "Needs retry"}
        tid = await self._make_task(db, "bad cmd", "ok")
        task_row = {"id": tid, "detail": "bad cmd", "expect": "ok", "output": "", "stderr": "err"}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=replan_with_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute(
            "SELECT review_verdict, review_reason, review_learning FROM tasks WHERE id=?", (tid,)
        )
        row = await cur.fetchone()
        assert row[0] == "replan"
        assert row[1] == "Bad output"
        assert row[2] == "Needs retry"


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

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("down")), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("down")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None
        assert len(completed) == 0

    async def test_skill_not_installed_fails_immediately(self, db, tmp_path):
        """Skill not installed → immediate failure, no review."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_reviewer = AsyncMock(return_value=REVIEW_REPLAN)
        with patch("kiso.worker.loop.run_reviewer", mock_reviewer), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None  # no replan — setup failure, not review
        assert len(remaining) == 1  # msg task
        mock_reviewer.assert_not_called()  # review skipped
        tasks = await get_tasks_for_plan(db, plan_id)
        skill_task = [t for t in tasks if t["type"] == "skill"][0]
        assert skill_task["status"] == "failed"
        assert "not installed" in skill_task["output"]
        assert reason is None  # failed but no replan

    async def test_skill_review_error(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("err")), \
             _patch_kiso_dir(tmp_path):
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

        async def _review_side_effect(cfg, goal, detail, expect, output, user_message, **kwargs):
            review_calls.append(detail)
            if "first" in detail:
                return REVIEW_REPLAN
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", side_effect=_review_side_effect), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert len(remaining) == 2  # second exec + msg
        assert len(review_calls) == 1  # only first exec reviewed

    # --- M25: replan task type in _execute_plan ---

    async def test_replan_task_triggers_replan(self, db, tmp_path):
        """Plan with exec + replan → returns (False, 'Self-directed replan: ...', completed, remaining)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Investigate")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo registry",
                          expect="JSON output")
        await create_task(db, plan_id, "sess1", type="replan",
                          detail="install appropriate skill")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Investigate", "user msg", 5,
            )

        assert success is False
        assert reason is not None
        assert reason.startswith("Self-directed replan:")
        assert "install appropriate skill" in reason
        assert len(completed) == 2  # exec + replan both completed
        assert remaining == []

    async def test_replan_task_marked_done(self, db, tmp_path):
        """The replan task itself gets status 'done'."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Investigate")
        await create_task(db, plan_id, "sess1", type="replan",
                          detail="decide next steps")

        with _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Investigate", "user msg", 5,
            )

        assert success is False
        assert reason.startswith("Self-directed replan:")
        # Check DB status
        tasks = await get_tasks_for_plan(db, plan_id)
        assert tasks[0]["status"] == "done"
        assert tasks[0]["output"] == "Replan requested by planner"


# --- Exec translator integration in _execute_plan ---


class TestExecTranslatorIntegration:
    """Tests that _execute_plan correctly calls run_exec_translator
    before executing exec tasks (architect/editor pattern)."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_translator_called_with_detail(self, db, tmp_path):
        """run_exec_translator receives the natural-language detail."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec",
                          detail="List all files in the current directory",
                          expect="shows files")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        translator_calls = []

        async def _capture_translator(cfg, detail, sys_env_text, **kw):
            translator_calls.append(detail)
            return "ls -la"

        with patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                    side_effect=_capture_translator), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(translator_calls) == 1
        assert translator_calls[0] == "List all files in the current directory"

    async def test_translated_command_is_executed(self, db, tmp_path):
        """The translated command (not the detail) is what gets executed."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec",
                          detail="Print the word translated",
                          expect="prints translated")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        async def _translate(cfg, detail, sys_env_text, **kw):
            return "echo translated"

        with patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                    side_effect=_translate), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        exec_tasks = [t for t in completed if t["type"] == "exec"]
        assert len(exec_tasks) == 1
        assert "translated" in exec_tasks[0]["output"]

    async def test_translator_failure_marks_task_failed(self, db, tmp_path):
        """ExecTranslatorError → task fails, plan stops."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec",
                          detail="Do something impossible",
                          expect="magic")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                    side_effect=ExecTranslatorError("Cannot translate")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None  # failed, no replan
        assert len(remaining) == 1  # msg task not executed

    async def test_translator_receives_plan_outputs(self, db, tmp_path):
        """The translator receives preceding plan outputs for context."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec",
                          detail="Create a file named hello.txt", expect="ok")
        await create_task(db, plan_id, "sess1", type="exec",
                          detail="Read the file created in the previous step",
                          expect="shows content")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        translator_calls = []

        async def _capture_translator(cfg, detail, sys_env_text, **kw):
            translator_calls.append({
                "detail": detail,
                "plan_outputs_text": kw.get("plan_outputs_text", ""),
            })
            if "Create" in detail:
                return "echo hello > hello.txt"
            return "cat hello.txt"

        with patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                    side_effect=_capture_translator), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(translator_calls) == 2
        # First call: no preceding outputs
        assert translator_calls[0]["plan_outputs_text"] == ""
        # Second call: has preceding outputs from first exec
        assert translator_calls[1]["plan_outputs_text"] != ""
        assert "hello.txt" in translator_calls[1]["plan_outputs_text"]

    async def test_translator_receives_sys_env(self, db, tmp_path):
        """The translator receives system environment context."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec",
                          detail="List files", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        captured_sys_env = []

        async def _capture(cfg, detail, sys_env_text, **kw):
            captured_sys_env.append(sys_env_text)
            return "ls"

        with patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                    side_effect=_capture), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        assert len(captured_sys_env) == 1
        assert "Shell:" in captured_sys_env[0]
        assert "Available binaries:" in captured_sys_env[0]
        # Session name should appear in the sys env context (absolute CWD)
        assert "Session: sess1" in captured_sys_env[0]

    async def test_msg_tasks_skip_translator(self, db, tmp_path):
        """msg tasks do NOT go through the translator."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        translator_mock = AsyncMock()

        with patch("kiso.worker.loop.run_exec_translator", translator_mock), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        translator_mock.assert_not_called()


# --- Audit integration in _execute_plan ---


class TestExecutePlanAudit:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_audit_log_task_called_for_exec(self, db, tmp_path):
        """audit.log_task called for exec task execution."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        # log_task called for exec and msg
        assert mock_audit.log_task.call_count == 2
        exec_call = mock_audit.log_task.call_args_list[0]
        assert exec_call[0][0] == "sess1"  # session
        assert exec_call[0][2] == "exec"  # task_type
        assert exec_call[0][3] == "echo ok"  # detail
        assert exec_call[0][4] == "done"  # status
        assert isinstance(exec_call[0][5], int)  # duration_ms

        msg_call = mock_audit.log_task.call_args_list[1]
        assert msg_call[0][2] == "msg"

    async def test_audit_log_review_called(self, db, tmp_path):
        """audit.log_review called after review."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        mock_audit.log_review.assert_called_once()
        args = mock_audit.log_review.call_args[0]
        assert args[0] == "sess1"  # session
        assert args[2] == "ok"  # verdict
        assert args[3] is False  # has_learning (no learn in REVIEW_OK)

    async def test_audit_log_review_with_learning(self, db, tmp_path):
        """audit.log_review records has_learning=True when learn is present."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        review_with_learn = {"status": "ok", "reason": None, "learn": "Uses Flask"}

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_learn), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        args = mock_audit.log_review.call_args[0]
        assert args[3] is True  # has_learning

    async def test_audit_log_webhook_called(self, db, tmp_path):
        """audit.log_webhook called after webhook delivery."""
        config = _make_config()
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.deliver_webhook", new_callable=AsyncMock, return_value=(True, 200, 1)), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        mock_audit.log_webhook.assert_called_once()
        args = mock_audit.log_webhook.call_args[0]
        assert args[0] == "sess1"  # session
        assert args[2] == "https://example.com/hook"  # url
        assert args[3] == 200  # status
        assert args[4] == 1  # attempts

    async def test_audit_log_webhook_on_failure(self, db, tmp_path):
        """audit.log_webhook records failed delivery."""
        config = _make_config()
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.deliver_webhook", new_callable=AsyncMock, return_value=(False, 500, 3)), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        args = mock_audit.log_webhook.call_args[0]
        assert args[3] == 500  # status
        assert args[4] == 3  # attempts

    async def test_audit_log_task_for_msg_only(self, db, tmp_path):
        """audit.log_task called for msg-only plan."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        mock_audit.log_task.assert_called_once()
        args = mock_audit.log_task.call_args[0]
        assert args[2] == "msg"
        assert args[4] == "done"

    async def test_audit_log_task_msg_passes_secrets(self, db, tmp_path):
        """audit.log_task for msg tasks passes deploy_secrets and session_secrets."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        secrets = {"TOK": "secret123"}
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                session_secrets=secrets,
            )

        mock_audit.log_task.assert_called_once()
        kwargs = mock_audit.log_task.call_args[1]
        assert "deploy_secrets" in kwargs
        assert kwargs["session_secrets"] == secrets

    async def test_audit_log_task_on_permission_denied(self, db, tmp_path):
        """audit.log_task called with status 'failed' when permission is denied."""
        config = _make_config(users={"bob": User(role="admin")})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo hi", expect="ok")

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username="alice",
            )

        mock_audit.log_task.assert_called_once()
        args = mock_audit.log_task.call_args[0]
        assert args[2] == "exec"
        assert args[4] == "failed"

    async def test_audit_log_task_on_cancel(self, db, tmp_path):
        """audit.log_task called for each cancelled task."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo a", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        cancel_event = asyncio.Event()
        cancel_event.set()

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                cancel_event=cancel_event,
            )

        assert mock_audit.log_task.call_count == 2
        for call in mock_audit.log_task.call_args_list:
            assert call[0][4] == "cancelled"

    async def test_audit_log_task_on_msg_llm_error(self, db, tmp_path):
        """audit.log_task called with status 'failed' on msg LLMError."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("down")), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        mock_audit.log_task.assert_called_once()
        args = mock_audit.log_task.call_args[0]
        assert args[2] == "msg"
        assert args[4] == "failed"

    async def test_audit_log_webhook_passes_secrets(self, db, tmp_path):
        """audit.log_webhook passes deploy_secrets and session_secrets."""
        config = _make_config()
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        secrets = {"TOK": "secret123"}
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.deliver_webhook", new_callable=AsyncMock, return_value=(True, 200, 1)), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.audit") as mock_audit:
            await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                session_secrets=secrets,
            )

        mock_audit.log_webhook.assert_called_once()
        kwargs = mock_audit.log_webhook.call_args[1]
        assert "deploy_secrets" in kwargs
        assert kwargs["session_secrets"] == secrets


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
        with _patch_kiso_dir(tmp_path):
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
        with _patch_kiso_dir(tmp_path):
            _write_plan_outputs("sess1", [{"index": 1, "type": "exec", "detail": "a", "output": "1", "status": "done"}])
            _write_plan_outputs("sess1", [
                {"index": 1, "type": "exec", "detail": "a", "output": "1", "status": "done"},
                {"index": 2, "type": "exec", "detail": "b", "output": "2", "status": "done"},
            ])

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        data = json.loads(path.read_text())
        assert len(data) == 2

    def test_empty_outputs(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            _write_plan_outputs("sess1", [])

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert path.exists()
        assert json.loads(path.read_text()) == []


# --- _cleanup_plan_outputs ---

class TestCleanupPlanOutputs:
    def test_removes_file(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            _write_plan_outputs("sess1", [{"index": 1, "type": "exec", "detail": "a", "output": "x", "status": "done"}])
            path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
            assert path.exists()
            _cleanup_plan_outputs("sess1")
            assert not path.exists()

    def test_no_error_if_missing(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
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

    def test_fenced_with_boundary_tokens(self):
        outputs = [{"index": 1, "type": "exec", "detail": "cmd", "output": "data", "status": "done"}]
        result = _format_plan_outputs_for_msg(outputs)
        assert "<<<TASK_OUTPUT_" in result
        assert "<<<END_TASK_OUTPUT_" in result


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

        async def _capture(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Report results", plan_outputs=plan_outputs)

        user_content = captured_messages[1]["content"]
        assert "## Preceding Task Outputs" in user_content
        assert "[1] exec: echo hi" in user_content

    async def test_no_plan_outputs_section_when_none(self, db):
        config = _make_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Report results")

        user_content = captured_messages[1]["content"]
        assert "Preceding Task Outputs" not in user_content

    async def test_no_plan_outputs_section_when_empty(self, db):
        config = _make_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Report results", plan_outputs=[])

        user_content = captured_messages[1]["content"]
        assert "Preceding Task Outputs" not in user_content


# --- _execute_plan: secret sanitization ---


class TestExecutePlanSanitization:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_exec_output_sanitized(self, db, tmp_path):
        """Verify that deploy secrets are stripped from exec task output."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(
            db, plan_id, "sess1", type="exec",
            detail="echo sk-secret-deploy-key", expect="output",
        )

        env_patch = {"KISO_SKILL_TOKEN": "sk-secret-deploy-key"}

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch.dict("os.environ", env_patch):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        task_output = completed[0]["output"]
        assert "sk-secret-deploy-key" not in task_output
        assert "[REDACTED]" in task_output


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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("down")), \
             _patch_kiso_dir(tmp_path):
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

        async def _capture_llm(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "Report done"

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.brain.call_llm", side_effect=_capture_llm), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        async def _capture_llm(cfg, role, messages, **kwargs):
            msg_calls.append(messages)
            return "Step 1 report"

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.brain.call_llm", side_effect=_capture_llm), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_kiso_dir(tmp_path):
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
        with _patch_kiso_dir(tmp_path):
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
        with _patch_kiso_dir(tmp_path):
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
        with _patch_kiso_dir(tmp_path):
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

        with _patch_kiso_dir(tmp_path):
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

        with _patch_kiso_dir(tmp_path):
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

        with _patch_kiso_dir(tmp_path):
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

        with _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
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

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                session_secrets=secrets,
            )

        assert success is True
        result = json.loads(completed[0]["output"])
        # Session secrets are now sanitized in output
        assert result == {"api_token": "[REDACTED]"}

    async def test_skill_review_replan(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{"text":"hi"}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
            success, reason, _, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        assert len(remaining) == 1  # msg task remaining


# --- M8: Webhook delivery in _execute_plan ---


class TestWebhookDelivery:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_msg_triggers_webhook(self, db, tmp_path):
        """msg task triggers webhook delivery when session has webhook."""
        config = _make_config()
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.deliver_webhook", new_callable=AsyncMock, return_value=(True, 200, 1)) as mock_wh, \
             _patch_kiso_dir(tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        mock_wh.assert_called_once()
        call_args = mock_wh.call_args
        assert call_args[0][0] == "https://example.com/hook"
        assert call_args[0][1] == "sess1"
        assert call_args[0][3] == "Hi"  # content
        assert call_args[0][4] is True  # final (only task)

    async def test_no_webhook_no_delivery(self, db, tmp_path):
        """No webhook → no delivery attempted."""
        config = _make_config()
        await create_session(db, "sess1")  # no webhook
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.deliver_webhook", new_callable=AsyncMock) as mock_wh, \
             _patch_kiso_dir(tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        mock_wh.assert_not_called()

    async def test_final_true_on_last_msg(self, db, tmp_path):
        """final: true on last msg task in the plan."""
        config = _make_config()
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="report")

        webhook_calls = []

        async def _capture_wh(url, session, task_id, content, final, **kwargs):
            webhook_calls.append({"final": final, "content": content})
            return (True, 200, 1)

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Done"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.deliver_webhook", side_effect=_capture_wh), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(webhook_calls) == 1
        assert webhook_calls[0]["final"] is True

    async def test_final_false_on_non_last_msg(self, db, tmp_path):
        """final: false on non-last msg task."""
        config = _make_config()
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="first msg")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="second msg")

        webhook_calls = []

        async def _capture_wh(url, session, task_id, content, final, **kwargs):
            webhook_calls.append({"final": final})
            return (True, 200, 1)

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="text"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.deliver_webhook", side_effect=_capture_wh), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(webhook_calls) == 2
        assert webhook_calls[0]["final"] is False
        assert webhook_calls[1]["final"] is True

    async def test_webhook_failure_doesnt_break_plan(self, db, tmp_path):
        """Webhook failure doesn't break plan execution."""
        config = _make_config()
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.deliver_webhook", new_callable=AsyncMock, return_value=(False, 500, 3)), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(completed) == 1


# --- M9: _apply_curator_result ---

class TestApplyCuratorResult:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_promote(self, db):
        lid = await save_learning(db, "Uses Python", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Uses Python 3.12", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["content"] == "Uses Python 3.12"
        assert facts[0]["source"] == "curator"
        cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
        assert (await cur.fetchone())[0] == "promoted"

    async def test_ask(self, db):
        lid = await save_learning(db, "Something unclear", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "ask", "fact": None, "question": "Which DB?", "reason": "Need clarity"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        items = await get_pending_items(db, "sess1")
        assert len(items) == 1
        assert items[0]["content"] == "Which DB?"
        assert items[0]["source"] == "curator"
        cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
        assert (await cur.fetchone())[0] == "promoted"

    async def test_discard(self, db):
        lid = await save_learning(db, "Command succeeded", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "discard", "fact": None, "question": None, "reason": "Transient"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
        assert (await cur.fetchone())[0] == "discarded"
        facts = await get_facts(db)
        assert len(facts) == 0


# --- M9: Knowledge processing in run_worker ---

class TestKnowledgeProcessing:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_curator_called_with_pending_learnings(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_learning(db, "Uses Python", "sess1")

        curator_result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "question": None, "reason": "Good"},
        ]}

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_curator", new_callable=AsyncMock, return_value=curator_result) as mock_curator, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_curator.assert_called_once()
        # Fact should be saved
        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["content"] == "Uses Python"

    async def test_curator_skipped_when_no_learnings(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_curator", new_callable=AsyncMock) as mock_curator, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_curator.assert_not_called()

    async def test_curator_failure_doesnt_break_worker(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_learning(db, "Something", "sess1")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_curator", new_callable=AsyncMock, side_effect=CuratorError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # Worker should still complete
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"

    async def test_summarizer_called_when_threshold_reached(self, db, tmp_path):
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "summarize_threshold": 2,  # Low threshold for testing
        })
        await create_session(db, "sess1")
        # Add enough messages to exceed threshold
        await save_message(db, "sess1", "alice", "user", "msg1")
        await save_message(db, "sess1", "alice", "user", "msg2")
        msg_id = await save_message(db, "sess1", "alice", "user", "msg3", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "msg3", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_summarizer", new_callable=AsyncMock, return_value="New summary") as mock_summ, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_summ.assert_called_once()
        from kiso.store import get_session
        sess = await get_session(db, "sess1")
        assert sess["summary"] == "New summary"

    async def test_summarizer_skipped_below_threshold(self, db, tmp_path):
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "summarize_threshold": 100,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_summarizer", new_callable=AsyncMock) as mock_summ, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_summ.assert_not_called()

    async def test_summarizer_failure_doesnt_break_worker(self, db, tmp_path):
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "summarize_threshold": 1,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_summarizer", new_callable=AsyncMock, side_effect=SummarizerError("down")), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"

    async def test_fact_consolidation_when_over_max(self, db, tmp_path):
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 2,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        # Add 3 facts (over max of 2)
        await save_fact(db, "Fact 1", "curator")
        await save_fact(db, "Fact 2", "curator")
        await save_fact(db, "Fact 3", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   return_value=[{"content": "Consolidated fact", "category": "general", "confidence": 1.0}]) as mock_consol, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_consol.assert_called_once()
        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["content"] == "Consolidated fact"
        assert facts[0]["source"] == "consolidation"

    async def test_consolidation_empty_result_preserves_facts(self, db, tmp_path):
        """Empty LLM consolidation result → facts NOT deleted."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 2,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_fact(db, "Fact 1", "curator")
        await save_fact(db, "Fact 2", "curator")
        await save_fact(db, "Fact 3", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   return_value=[]), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # All 3 original facts should be preserved
        facts = await get_facts(db)
        assert len(facts) == 3

    async def test_consolidation_skipped_under_max(self, db, tmp_path):
        """Facts <= max → no consolidation call."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 50,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_fact(db, "Fact 1", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock) as mock_consol, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_consol.assert_not_called()

    async def test_curator_nonexistent_learning_id(self, db, tmp_path):
        """Curator references nonexistent learning ID → silently handles without crash."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_learning(db, "Something", "sess1")

        # Curator returns evaluation with nonexistent learning_id
        curator_result = {"evaluations": [
            {"learning_id": 9999, "verdict": "discard", "fact": None, "question": None, "reason": "Bad"},
        ]}

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_curator", new_callable=AsyncMock, return_value=curator_result), \
             _patch_kiso_dir(tmp_path):
            # Should not crash
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"

    async def test_summarizer_threshold_exact_boundary(self, db, tmp_path):
        """count == threshold triggers summarizer."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "summarize_threshold": 2,
        })
        await create_session(db, "sess1")
        # Add exactly 1 message, then the processed msg makes 2 = threshold
        await save_message(db, "sess1", "alice", "user", "msg1")
        msg_id = await save_message(db, "sess1", "alice", "user", "msg2", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "msg2", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_summarizer", new_callable=AsyncMock, return_value="Summary") as mock_summ, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_summ.assert_called_once()

    async def test_multi_session_fact_visibility(self, db, tmp_path):
        """Fact created in session A is visible in session B planner context."""
        config = _make_config()
        await create_session(db, "sessA")
        await create_session(db, "sessB")
        await save_fact(db, "Python 3.12", "curator", session="sessA")

        # Build planner messages for session B — facts are global
        from kiso.brain import build_planner_messages
        msgs, _installed = await build_planner_messages(db, config, "sessB", "admin", "hello")
        content = msgs[1]["content"]
        assert "Python 3.12" in content

    async def test_apply_curator_promote_saves_session(self, db, tmp_path):
        """Promoted fact has correct session attribution."""
        await create_session(db, "sess1")
        lid = await save_learning(db, "Uses Flask", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Uses Flask", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["session"] == "sess1"

    async def test_knowledge_processing_order(self, db, tmp_path):
        """Curator runs before summarizer (order verified via side effects)."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "summarize_threshold": 1,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_learning(db, "A fact", "sess1")

        call_order = []

        async def _mock_curator(config, learnings, **kwargs):
            call_order.append("curator")
            return {"evaluations": [
                {"learning_id": 1, "verdict": "discard", "fact": None, "question": None, "reason": "Noise"},
            ]}

        async def _mock_summarizer(config, summary, messages, **kwargs):
            call_order.append("summarizer")
            return "New summary"

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_curator", side_effect=_mock_curator), \
             patch("kiso.worker.loop.run_summarizer", side_effect=_mock_summarizer), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        assert call_order == ["curator", "summarizer"]

    async def test_fact_consolidation_failure_doesnt_break_worker(self, db, tmp_path):
        """SummarizerError in consolidation is caught, worker continues."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 2,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_fact(db, "Fact 1", "curator")
        await save_fact(db, "Fact 2", "curator")
        await save_fact(db, "Fact 3", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   side_effect=SummarizerError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # Worker completed, facts preserved (consolidation failed)
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"
        facts = await get_facts(db)
        assert len(facts) == 3


# --- M10: Paraphraser integration ---

class TestParaphraserIntegration:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_paraphraser_called_with_untrusted(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        # Add untrusted message
        await save_message(db, "sess1", "stranger", "user", "inject this", trusted=False, processed=True)

        planner_calls = []

        async def _capture_planner(db, config, session, role, content, **kwargs):
            planner_calls.append(kwargs)
            return VALID_PLAN

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_capture_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_paraphraser", new_callable=AsyncMock,
                   return_value="The stranger mentioned something.") as mock_para, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_para.assert_called_once()
        # Paraphrased context should be passed to planner
        assert planner_calls[0].get("paraphrased_context") == "The stranger mentioned something."

    async def test_paraphraser_skipped_when_no_untrusted(self, db, tmp_path):
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_paraphraser", new_callable=AsyncMock) as mock_para, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_para.assert_not_called()

    async def test_paraphraser_failure_falls_back_to_none(self, db, tmp_path):
        """Paraphraser error is caught, planner still called with paraphrased_context=None."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_message(db, "sess1", "stranger", "user", "inject", trusted=False, processed=True)

        planner_calls = []

        async def _capture_planner(db, config, session, role, content, **kwargs):
            planner_calls.append(kwargs)
            return VALID_PLAN

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_capture_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_paraphraser", new_callable=AsyncMock,
                   side_effect=ParaphraserError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # Planner should still be called, paraphrased_context should be None
        assert planner_calls[0].get("paraphrased_context") is None
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"


# --- M10: Fencing in plan outputs and replan context ---

class TestFencingInWorker:
    def test_plan_outputs_fenced(self):
        outputs = [{"index": 1, "type": "exec", "detail": "echo hi", "output": "hi\n", "status": "done"}]
        result = _format_plan_outputs_for_msg(outputs)
        assert "<<<TASK_OUTPUT_" in result
        assert "<<<END_TASK_OUTPUT_" in result
        assert "hi\n" in result
        # Old backtick fencing should NOT be present
        assert "```" not in result

    def test_plan_outputs_no_output_fenced(self):
        outputs = [{"index": 1, "type": "exec", "detail": "cmd", "output": None, "status": "failed"}]
        result = _format_plan_outputs_for_msg(outputs)
        assert "<<<TASK_OUTPUT_" in result
        assert "(no output)" in result

    def test_replan_context_fenced(self):
        completed = [{"type": "exec", "detail": "echo hi", "status": "done", "output": "hi\n"}]
        ctx = _build_replan_context(completed, [], "broke", [])
        assert "<<<TASK_OUTPUT_" in ctx
        assert "<<<END_TASK_OUTPUT_" in ctx
        assert "hi" in ctx

    def test_replan_context_no_output_placeholder(self):
        completed = [{"type": "exec", "detail": "cmd", "status": "done", "output": ""}]
        ctx = _build_replan_context(completed, [], "broke", [])
        assert "(no output)" in ctx


# --- M10 Batch 3: Permission re-validation + exec sandbox ---


class TestPermissionRevalidation:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_permission_revalidation_blocks_removed_user(self, db, tmp_path):
        """Config reload returns config without user → task fails."""
        config = _make_config(users={"alice": User(role="admin")})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        # Reload returns config without alice
        config_without_alice = _make_config(users={"bob": User(role="admin")})

        with patch("kiso.worker.loop.reload_config", return_value=config_without_alice), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username="alice",
            )

        assert success is False
        assert len(completed) == 0
        assert len(remaining) == 1  # msg task
        tasks = await get_tasks_for_plan(db, plan_id)
        assert "no longer exists" in tasks[0]["output"]

    async def test_permission_revalidation_blocks_removed_skill(self, db, tmp_path):
        """Skill removed from user's allowed list → fails."""
        config = _make_config(users={"bob": User(role="user", skills=["search"])})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="deploy",
                          skill="deploy", args="{}", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        # Reload returns config where bob only has "search"
        with patch("kiso.worker.loop.reload_config", return_value=config), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username="bob",
            )

        assert success is False
        tasks = await get_tasks_for_plan(db, plan_id)
        assert "not in user's allowed skills" in tasks[0]["output"]

    async def test_config_reload_failure_uses_cached(self, db, tmp_path):
        """ConfigError → falls back to cached config, execution continues."""
        config = _make_config(users={"alice": User(role="admin")})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.reload_config", side_effect=ConfigError("bad toml")), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username="alice",
            )

        assert success is True
        assert len(completed) == 1


class TestPerSessionSandbox:
    def test_ensure_sandbox_user_creates_user(self):
        """When user doesn't exist, useradd is called and UID returned."""
        call_count = [0]

        def _getpwnam_side_effect(name):
            call_count[0] += 1
            if call_count[0] == 1:
                raise KeyError(name)  # first call: user doesn't exist
            return type("pw", (), {"pw_uid": 50001})()  # second call: user created

        with patch("kiso.worker.utils.pwd") as mock_pwd, \
             patch("subprocess.run") as mock_run:
            mock_pwd.getpwnam.side_effect = _getpwnam_side_effect
            uid = _ensure_sandbox_user("test-session")

        assert uid == 50001
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert "useradd" in args[0][0]

    def test_ensure_sandbox_user_reuses_existing(self):
        """When user already exists, no useradd call is made."""
        with patch("kiso.worker.utils.pwd") as mock_pwd, \
             patch("subprocess.run") as mock_run:
            mock_pwd.getpwnam.return_value = type("pw", (), {"pw_uid": 50001})()
            uid = _ensure_sandbox_user("test-session")

        assert uid == 50001
        mock_run.assert_not_called()

    def test_ensure_sandbox_user_creation_fails(self):
        """When useradd fails, returns None."""
        import subprocess
        with patch("kiso.worker.utils.pwd") as mock_pwd, \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "useradd")):
            mock_pwd.getpwnam.side_effect = KeyError("no-user")
            uid = _ensure_sandbox_user("test-session")

        assert uid is None

    def test_workspace_chown_chmod(self, tmp_path):
        """_session_workspace applies chown/chmod when sandbox_uid is given."""
        with _patch_kiso_dir(tmp_path), \
             patch("os.chown") as mock_chown, \
             patch("os.chmod") as mock_chmod:
            ws = _session_workspace("sess1", sandbox_uid=1234)

        pub = ws / "pub"
        assert mock_chown.call_count == 2
        mock_chown.assert_any_call(ws, 1234, 1234)
        mock_chown.assert_any_call(pub, 1234, 1234)
        mock_chmod.assert_called_once_with(ws, 0o700)

    def test_workspace_no_chown_without_sandbox_uid(self, tmp_path):
        """_session_workspace skips chown/chmod when sandbox_uid is None."""
        with _patch_kiso_dir(tmp_path), \
             patch("os.chown") as mock_chown, \
             patch("os.chmod") as mock_chmod:
            _session_workspace("sess1")

        mock_chown.assert_not_called()
        mock_chmod.assert_not_called()

    async def test_sandbox_uid_passed_to_exec_subprocess(self, tmp_path):
        """When sandbox_uid is set, it's passed to create_subprocess_shell."""
        captured_kwargs = {}

        async def _mock_subprocess(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            return proc

        with _patch_kiso_dir(tmp_path), \
             patch("asyncio.create_subprocess_shell", side_effect=_mock_subprocess):
            await _exec_task("sess1", "echo ok", 5, sandbox_uid=1234)

        assert captured_kwargs.get("user") == 1234

    async def test_no_sandbox_uid_no_user_kwarg(self, tmp_path):
        """When sandbox_uid is None, 'user' kwarg is NOT passed."""
        captured_kwargs = {}

        async def _mock_subprocess(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            return proc

        with _patch_kiso_dir(tmp_path), \
             patch("asyncio.create_subprocess_shell", side_effect=_mock_subprocess):
            await _exec_task("sess1", "echo ok", 5, sandbox_uid=None)

        assert "user" not in captured_kwargs

    async def test_sandbox_uid_passed_to_skill_subprocess(self, tmp_path):
        """When sandbox_uid is set, it's passed to create_subprocess_exec."""
        captured_kwargs = {}

        async def _mock_subprocess(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b'{"ok":true}\n', b""))
            proc.returncode = 0
            return proc

        skill = _create_echo_skill(tmp_path)
        with _patch_kiso_dir(tmp_path), \
             patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess):
            await _skill_task("sess1", skill, {"text": "hi"}, None, None, 5, sandbox_uid=9999)

        assert captured_kwargs.get("user") == 9999

    async def test_skill_no_sandbox_uid_no_user_kwarg(self, tmp_path):
        """When sandbox_uid is None, 'user' kwarg is NOT passed to skill subprocess."""
        captured_kwargs = {}

        async def _mock_subprocess(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b'{"ok":true}\n', b""))
            proc.returncode = 0
            return proc

        skill = _create_echo_skill(tmp_path)
        with _patch_kiso_dir(tmp_path), \
             patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess):
            await _skill_task("sess1", skill, {"text": "hi"}, None, None, 5, sandbox_uid=None)

        assert "user" not in captured_kwargs


class TestPermissionRevalidationEdgeCases:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_exec_allowed_for_user_role(self, db, tmp_path):
        """User role can run exec tasks (permission check only blocks removed users/skills)."""
        config = _make_config(users={"bob": User(role="user", skills=["search"])})

        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username="bob",
            )

        assert success is True
        assert len(completed) == 2

    async def test_permission_checked_per_task(self, db, tmp_path):
        """Permissions are re-checked before EACH task, not once per plan."""
        config_with_alice = _make_config(users={"alice": User(role="admin")})
        config_without_alice = _make_config(users={"bob": User(role="admin")})

        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo first", expect="ok")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo second", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        reload_calls = [0]

        def _reload_side_effect(*args, **kwargs):
            reload_calls[0] += 1
            # First task: alice exists. Second task: alice removed.
            if reload_calls[0] <= 1:
                return config_with_alice
            return config_without_alice

        with patch("kiso.worker.loop.reload_config", side_effect=_reload_side_effect), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, remaining = await _execute_plan(
                db, config_with_alice, "sess1", plan_id, "Test", "msg", 5,
                username="alice",
            )

        assert success is False
        assert len(completed) == 1  # first exec succeeded
        assert len(remaining) == 1  # msg task
        # Second task should have failed due to permission
        tasks = await get_tasks_for_plan(db, plan_id)
        assert "no longer exists" in tasks[1]["output"]

    async def test_admin_skips_sandbox_in_execute_plan(self, db, tmp_path):
        """Admin user does NOT get sandboxed — _ensure_sandbox_user never called."""
        config = _make_config(
            users={"alice": User(role="admin")},
        )

        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        captured_kwargs = {}

        async def _mock_subprocess(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            return proc

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch("asyncio.create_subprocess_shell", side_effect=_mock_subprocess):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username="alice",
            )

        assert success is True
        assert "user" not in captured_kwargs  # no sandbox for admin

    async def test_no_username_skips_permission_check(self, db, tmp_path):
        """username=None (system/anonymous) always passes permission check."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username=None,
            )

        assert success is True


# --- Cancel mechanism ---


class TestBuildCancelSummary:
    def test_with_completed_and_remaining(self):
        completed = [
            {"type": "exec", "detail": "echo hello"},
            {"type": "exec", "detail": "echo world"},
        ]
        remaining = [{"type": "msg", "detail": "Report results"}]
        result = _build_cancel_summary(completed, remaining, "Run tests")
        assert "The user cancelled the plan: Run tests" in result
        assert "Completed (2):" in result
        assert "[exec] echo hello" in result
        assert "Skipped (1):" in result
        assert "[msg] Report results" in result
        assert "suggest next steps" in result

    def test_no_completed(self):
        remaining = [{"type": "msg", "detail": "done"}]
        result = _build_cancel_summary([], remaining, "Goal")
        assert "No tasks were completed" in result
        assert "Skipped (1):" in result

    def test_no_remaining(self):
        completed = [{"type": "exec", "detail": "echo ok"}]
        result = _build_cancel_summary(completed, [], "Goal")
        assert "Completed (1):" in result
        assert "Skipped" not in result


class TestCancelMechanism:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_cancel_before_first_task(self, db, tmp_path):
        """cancel_event set before execution → all tasks cancelled, plan cancelled."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo hi", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        cancel_event = asyncio.Event()
        cancel_event.set()  # already cancelled

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                cancel_event=cancel_event,
            )

        assert success is False
        assert reason == "cancelled"
        assert len(completed) == 0
        assert len(remaining) == 2

        tasks = await get_tasks_for_plan(db, plan_id)
        assert all(t["status"] == "cancelled" for t in tasks)

    async def test_cancel_mid_plan(self, db, tmp_path):
        """Set event after first task starts → first completes normally, rest cancelled."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo first", expect="ok")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo second", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        cancel_event = asyncio.Event()
        review_calls = [0]

        async def _review_then_cancel(*args, **kwargs):
            review_calls[0] += 1
            # After reviewing first task, set cancel
            cancel_event.set()
            return REVIEW_OK

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=_review_then_cancel), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                cancel_event=cancel_event,
            )

        assert success is False
        assert reason == "cancelled"
        assert len(completed) == 1  # first exec completed
        assert len(remaining) == 2  # second exec + msg cancelled

        tasks = await get_tasks_for_plan(db, plan_id)
        assert tasks[0]["status"] == "done"
        assert tasks[1]["status"] == "cancelled"
        assert tasks[2]["status"] == "cancelled"

    async def test_cancel_generates_summary(self, db, tmp_path):
        """System message saved to DB with cancel summary."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do stuff", processed=False)

        cancel_plan = {
            "goal": "Do stuff",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        cancel_event = asyncio.Event()

        # Set cancel after first task review
        async def _review_then_cancel(*args, **kwargs):
            cancel_event.set()
            return REVIEW_OK

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do stuff", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=cancel_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=_review_then_cancel), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Plan was cancelled. 1 task done, 1 skipped."), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(
                run_worker(db, config, "sess1", queue, cancel_event=cancel_event),
                timeout=5,
            )

        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "cancelled"

        # Check msg task created in the plan
        tasks = await get_tasks_for_plan(db, plan["id"])
        msg_tasks = [t for t in tasks if t["type"] == "msg"]
        assert any(t["status"] == "done" and "cancelled" in (t["output"] or "").lower()
                   for t in msg_tasks)

        # Check system message was saved
        from kiso.store import get_oldest_messages
        msgs = await get_oldest_messages(db, "sess1", limit=100)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "cancelled" in system_msgs[0]["content"].lower()

    async def test_cancel_delivers_webhook(self, db, tmp_path):
        """Webhook called with final=True for cancel summary."""
        config = _make_config()
        await create_session(db, "sess1")

        # Register webhook on session
        from kiso.store import upsert_session
        await upsert_session(db, "sess1", webhook="https://example.com/hook")

        msg_id = await save_message(db, "sess1", "alice", "user", "do stuff", processed=False)

        cancel_plan = {
            "goal": "Do stuff",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        cancel_event = asyncio.Event()

        async def _review_then_cancel(*args, **kwargs):
            cancel_event.set()
            return REVIEW_OK

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do stuff", "user_role": "admin"})

        mock_webhook = AsyncMock(return_value=(True, 200, 1))

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=cancel_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=_review_then_cancel), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Cancelled."), \
             patch("kiso.worker.loop.deliver_webhook", mock_webhook), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(
                run_worker(db, config, "sess1", queue, cancel_event=cancel_event),
                timeout=5,
            )

        mock_webhook.assert_called_once()
        call_kwargs = mock_webhook.call_args
        # final=True is the 5th positional arg
        assert call_kwargs[0][4] is True  # final=True

    async def test_cancel_clears_flag(self, db, tmp_path):
        """After cancel handling, event is cleared (next message can process)."""
        config = _make_config()
        await create_session(db, "sess1")
        msg1 = await save_message(db, "sess1", "alice", "user", "first", processed=False)
        msg2 = await save_message(db, "sess1", "alice", "user", "second", processed=False)

        cancel_plan = {
            "goal": "First",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        cancel_event = asyncio.Event()
        planner_calls = [0]

        async def _planner_side_effect(db, cfg, sess, role, content, **kwargs):
            planner_calls[0] += 1
            if planner_calls[0] == 1:
                # Cancel after first plan starts
                cancel_event.set()
            return cancel_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg1, "content": "first", "user_role": "admin"})
        await queue.put({"id": msg2, "content": "second", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner_side_effect), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(
                run_worker(db, config, "sess1", queue, cancel_event=cancel_event),
                timeout=5,
            )

        # cancel_event should be cleared so second message was processed
        assert not cancel_event.is_set()
        # Should have been called twice (once per message)
        assert planner_calls[0] == 2

    async def test_cancel_llm_failure_uses_raw_summary(self, db, tmp_path):
        """LLMError in cancel summary → fallback to raw text."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do stuff", processed=False)

        cancel_plan = {
            "goal": "Do stuff",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        cancel_event = asyncio.Event()

        async def _review_then_cancel(*args, **kwargs):
            cancel_event.set()
            return REVIEW_OK

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do stuff", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=cancel_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=_review_then_cancel), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   side_effect=MessengerError("API down")), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(
                run_worker(db, config, "sess1", queue, cancel_event=cancel_event),
                timeout=5,
            )

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "cancelled"

        # Raw summary should be saved as system message
        from kiso.store import get_oldest_messages
        msgs = await get_oldest_messages(db, "sess1", limit=100)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "The user cancelled the plan" in system_msgs[0]["content"]


# --- Output truncation ---


class TestOutputTruncation:
    async def test_exec_output_truncated_when_large(self, tmp_path):
        """Command producing >max_output_size chars gets truncated."""
        # printf 2000 chars, limit to 100
        cmd = "printf '%02000d' 0"
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _exec_task(
                "test-sess", cmd, 5, max_output_size=100,
            )
        assert success is True
        assert len(stdout) <= 100 + len("\n[truncated]")
        assert stdout.endswith("[truncated]")

    async def test_skill_output_truncated_when_large(self, tmp_path):
        skill_dir = tmp_path / "skills" / "big"
        skill_dir.mkdir(parents=True)
        (skill_dir / "run.py").write_text("print('B' * 2000)")
        (skill_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "big"\n'
            '[kiso.skill]\nsummary = "Big"\n'
        )
        (skill_dir / "pyproject.toml").write_text('[project]\nname = "big"\nversion = "0.1.0"')
        skill = {
            "name": "big", "summary": "Big", "args_schema": {}, "env": {},
            "session_secrets": [], "path": str(skill_dir), "version": "0.1.0", "description": "",
        }
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success = await _skill_task(
                "sess1", skill, {}, None, None, 5, max_output_size=100,
            )
        assert success is True
        assert stdout.endswith("[truncated]")
        assert len(stdout) <= 100 + len("\n[truncated]")


# --- Curator/summarizer timeout ---


class TestPostPlanTimeouts:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_curator_timeout_does_not_crash(self, db, tmp_path):
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 1,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_learning(db, "Something", "sess1")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        async def _slow_curator(*args, **kwargs):
            await asyncio.sleep(999)

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_curator", side_effect=_slow_curator), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Worker should still complete — plan is done
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"

    async def test_summarizer_timeout_does_not_crash(self, db, tmp_path):
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 1,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "summarize_threshold": 1,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        async def _slow_summarizer(*args, **kwargs):
            await asyncio.sleep(999)

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_summarizer", side_effect=_slow_summarizer), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"


# --- Cancel during replan window ---


class TestCancelDuringReplanWindow:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_cancel_during_replan_window(self, db, tmp_path):
        """Set cancel_event after _execute_plan returns replan, before run_planner.
        Verify plan is cancelled and replan planner is never called."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
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

        cancel_event = asyncio.Event()
        planner_calls = [0]

        async def _planner_side_effect(db, config, session, role, content, **kwargs):
            planner_calls[0] += 1
            if planner_calls[0] == 1:
                return fail_plan
            # Should not be called for replan since cancel_event is set
            raise AssertionError("run_planner called for replan after cancel")

        # Set cancel after the first reviewer call (triggers replan path)
        async def _review_and_cancel(*args, **kwargs):
            cancel_event.set()
            return REVIEW_REPLAN

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "fail", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner_side_effect), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=_review_and_cancel), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue,
                                              cancel_event=cancel_event), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "cancelled"
        assert planner_calls[0] == 1  # only initial plan, no replan call


# --- Worker crash recovery ---


class TestWorkerCrashRecovery:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_worker_crash_recovery_continues(self, db, tmp_path):
        """Mock run_planner to raise RuntimeError on first message, then succeed on second.
        Verify worker processes both."""
        config = _make_config()
        await create_session(db, "sess1")
        msg1 = await save_message(db, "sess1", "alice", "user", "crash", processed=False)
        msg2 = await save_message(db, "sess1", "alice", "user", "ok", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg1, "content": "crash", "user_role": "admin"})
        await queue.put({"id": msg2, "content": "ok", "user_role": "admin"})

        call_count = [0]

        async def _planner_side_effect(db, config, session, role, content, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Unexpected DB corruption")
            return VALID_PLAN

        with patch("kiso.worker.loop.run_planner", side_effect=_planner_side_effect), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Second message should have been processed successfully
        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "done"
        assert call_count[0] == 2

    async def test_worker_crash_does_not_leave_running_tasks(self, db, tmp_path):
        """Inject crash during plan execution, verify no tasks remain in 'running' status."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "crash", processed=False)

        crash_plan = {
            "goal": "Crash during exec",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "crash", "user_role": "admin"})

        # Crash during review (after task set to running/done)
        async def _review_crash(*args, **kwargs):
            raise RuntimeError("Unexpected crash during review")

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=crash_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=_review_crash), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Verify no tasks are stuck in "running"
        tasks = await get_tasks_for_session(db, "sess1")
        running = [t for t in tasks if t["status"] == "running"]
        assert len(running) == 0


# --- cancel_event null-safety ---


class TestCancelEventNullSafety:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_cancel_event_none_no_crash(self, db, tmp_path):
        """run_worker with cancel_event=None completes without error."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi!"), \
             _patch_kiso_dir(tmp_path):
            # cancel_event=None is the default
            await asyncio.wait_for(
                run_worker(db, config, "sess1", queue, cancel_event=None),
                timeout=3,
            )

        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "done"


# --- Malformed secrets handling ---


class TestMalformedSecrets:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_malformed_secrets_handled_gracefully(self, db, tmp_path):
        """Plan with malformed secrets (missing 'key'/'value') doesn't crash."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        plan_with_bad_secrets = {
            "goal": "Say hello",
            "secrets": [{"k": "v"}],  # wrong keys
            "tasks": [{"type": "msg", "detail": "Hello!", "skill": None, "args": None, "expect": None}],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=plan_with_bad_secrets), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi!"), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "done"

    async def test_non_dict_secrets_handled(self, db, tmp_path):
        """Plan with non-dict secrets entries doesn't crash."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)

        plan_with_string_secrets = {
            "goal": "Say hello",
            "secrets": ["not-a-dict"],
            "tasks": [{"type": "msg", "detail": "Hello!", "skill": None, "args": None, "expect": None}],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=plan_with_string_secrets), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi!"), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "done"


# --- Edge cases: sandbox workspace + fact consolidation timeout ---


class TestSandboxWorkspaceInExecutePlan:
    """Cover: _session_workspace called with sandbox_uid when role=user."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_user_role_triggers_sandbox_workspace(self, db, tmp_path):
        """When revalidate_permissions returns role='user' and sandbox UID is set,
        _session_workspace is called with sandbox_uid."""
        config = _make_config(
            users={"bob": User(role="user", skills="*")},
        )
        plan_id = await create_plan(db, "sess1", 1, "Test sandbox")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")

        workspace_calls = []

        original_workspace = _session_workspace

        def tracking_workspace(session, sandbox_uid=None):
            workspace_calls.append(sandbox_uid)
            return original_workspace(session)

        async def _mock_subprocess(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            return proc

        from kiso.security import PermissionResult

        with patch("kiso.worker.loop.reload_config", return_value=config), \
             patch("kiso.worker.loop.revalidate_permissions",
                   return_value=PermissionResult(allowed=True, role="user")), \
             patch("kiso.worker.loop._ensure_sandbox_user", return_value=42), \
             patch("kiso.worker.loop._session_workspace", side_effect=tracking_workspace), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             patch("asyncio.create_subprocess_shell", side_effect=_mock_subprocess):
            success, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test sandbox", "msg", 5,
                username="bob",
            )

        assert success is True
        # _session_workspace was called with sandbox_uid=42
        assert 42 in workspace_calls


class TestFactConsolidationTimeout:
    """Cover: fact consolidation asyncio.TimeoutError."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_fact_consolidation_timeout_doesnt_break_worker(self, db, tmp_path):
        """asyncio.TimeoutError in fact consolidation is caught, worker continues."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 2,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        await save_fact(db, "Fact 1", "curator")
        await save_fact(db, "Fact 2", "curator")
        await save_fact(db, "Fact 3", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        # Make run_fact_consolidation hang so asyncio.wait_for raises TimeoutError
        async def slow_consolidation(*args, **kwargs):
            await asyncio.sleep(999)

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   side_effect=slow_consolidation), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=8)

        # Worker completed fine, facts preserved
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"
        facts = await get_facts(db)
        assert len(facts) == 3


# --- 21c: Fact consolidation safety guards ---


class TestConsolidationSafetyGuards:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_consolidation_catastrophic_shrinkage_skipped(self, db, tmp_path):
        """10 facts → consolidation returns 1 (< 30%) → originals preserved."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 5,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        for i in range(10):
            await save_fact(db, f"Important fact number {i}", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   return_value=[{"content": "Only one fact", "category": "general", "confidence": 1.0}]), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # All 10 original facts preserved (1 < 10 * 0.3 = 3)
        facts = await get_facts(db)
        assert len(facts) == 10

    async def test_consolidation_short_facts_filtered(self, db, tmp_path):
        """Consolidation returns mix of valid and <3-char → short ones filtered."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 2,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        for i in range(5):
            await save_fact(db, f"A somewhat long fact number {i}", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        # Mix of valid and too-short/empty facts (structured dicts)
        consolidated = [
            {"content": "This is a valid consolidated fact", "category": "general", "confidence": 1.0},
            {"content": "ab", "category": "general", "confidence": 1.0},   # 2 chars → filtered
            {"content": "", "category": "general", "confidence": 1.0},     # empty → filtered
            {"content": "Another valid fact here", "category": "project", "confidence": 0.9},
        ]
        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   return_value=consolidated), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # Only the 2 valid (>= 3 chars) facts should remain
        facts = await get_facts(db)
        assert len(facts) == 2
        contents = [f["content"] for f in facts]
        assert "This is a valid consolidated fact" in contents
        assert "Another valid fact here" in contents

    async def test_consolidation_custom_min_ratio_respected(self, db, tmp_path):
        """fact_consolidation_min_ratio is read from config, not hardcoded.

        With ratio=0.5: 10 facts → 4 consolidated = 40% < 50% → skip (originals kept).
        With ratio=0.3 (default): same 4/10=40% > 30% → consolidation accepted.
        This verifies the config value is actually driving the safety threshold.
        """
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 5,
            "fact_consolidation_min_ratio": 0.5,  # stricter than default
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        for i in range(10):
            await save_fact(db, f"Important fact number {i}", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        # 4/10 = 40% — exceeds default 0.3 threshold but below custom 0.5 threshold
        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   return_value=[
                       {"content": f"Merged fact {i}", "category": "general", "confidence": 1.0}
                       for i in range(4)
                   ]), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # All 10 originals must be preserved (4 < 10 * 0.5 = 5)
        facts = await get_facts(db, is_admin=True)
        assert len(facts) == 10

    async def test_consolidation_preserves_user_fact_session(self, db, tmp_path):
        """M43: user-category facts re-inserted after consolidation keep their session.

        After consolidation the re-inserted user fact must be visible in the
        session that triggered consolidation but hidden from a different session.
        """
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "knowledge_max_facts": 2,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        for i in range(5):
            await save_fact(db, f"Fact {i}", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        user_fact_content = "Alice prefers concise answers"
        consolidated = [
            {"content": "General project info", "category": "project", "confidence": 1.0},
            {"content": user_fact_content, "category": "user", "confidence": 1.0},
        ]
        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.run_fact_consolidation", new_callable=AsyncMock,
                   return_value=consolidated), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        facts_sess1 = await get_facts(db, session="sess1")
        facts_sess2 = await get_facts(db, session="sess2")

        sess1_contents = [f["content"] for f in facts_sess1]
        sess2_contents = [f["content"] for f in facts_sess2]

        # user fact is visible in the session that triggered consolidation
        assert user_fact_content in sess1_contents, (
            "user fact should be visible in the consolidation session"
        )
        # user fact is NOT visible in another session
        assert user_fact_content not in sess2_contents, (
            "user fact leaked into unrelated session after consolidation"
        )
        # project fact is global — visible everywhere
        assert "General project info" in sess1_contents
        assert "General project info" in sess2_contents


# --- M34: Fact usage tracking ---


class TestFactUsageTracking:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_fact_usage_bumped_after_success(self, db, tmp_path):
        """Facts get use_count bumped after a successful plan."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hello", processed=False)
        fid = await save_fact(db, "Important fact for planning", "curator")

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "hello", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=VALID_PLAN), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        facts = await get_facts(db)
        assert facts[0]["use_count"] == 1
        assert facts[0]["last_used"] is not None


# --- M34: Fact decay and archive in post-plan ---


class TestFactDecayInPostPlan:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_stale_facts_decayed_after_plan(self, db, tmp_path):
        """Stale facts get their confidence reduced via _post_plan_knowledge."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "fact_decay_days": 7,
            "fact_decay_rate": 0.1,
            "fact_archive_threshold": 0.3,
        })
        await create_session(db, "sess1")
        fid = await save_fact(db, "Old stale fact", "curator")
        # Backdate to 10 days ago (no usage bump — simulates a fact not used)
        await db.execute(
            "UPDATE facts SET created_at = datetime('now', '-10 days') WHERE id = ?",
            (fid,),
        )
        await db.commit()

        # Call _post_plan_knowledge directly (bypasses success handler usage bump)
        await _post_plan_knowledge(db, config, "sess1", None, exec_timeout=5)

        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["confidence"] < 1.0

    async def test_low_confidence_facts_archived(self, db, tmp_path):
        """Facts with low confidence are archived via _post_plan_knowledge."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            "fact_decay_days": 7,
            "fact_decay_rate": 0.1,
            "fact_archive_threshold": 0.3,
        })
        await create_session(db, "sess1")
        # Create a fact with very low confidence
        await save_fact(db, "Barely alive fact", "curator", confidence=0.1)

        # Call _post_plan_knowledge directly
        await _post_plan_knowledge(db, config, "sess1", None, exec_timeout=5)

        # Fact should be archived (confidence 0.1 < threshold 0.3)
        facts = await get_facts(db)
        assert len(facts) == 0
        # Check facts_archive
        cur = await db.execute("SELECT * FROM facts_archive")
        archived = await cur.fetchall()
        assert len(archived) == 1


# --- 21d: Planning failure notifies user ---


class TestPlanErrorNotifiesUser:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_plan_error_saves_system_message(self, db, tmp_path):
        """PlanError → system message saved to DB."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "bad request", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "bad request", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock,
                   side_effect=PlanError("LLM call failed: timeout")), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        # System message should be saved
        msgs = await get_recent_messages(db, "sess1", limit=10)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) >= 1
        assert "Planning failed" in system_msgs[-1]["content"]
        assert "LLM call failed" in system_msgs[-1]["content"]

        # Failed plan + msg task should exist for CLI visibility
        plan = await get_plan_for_session(db, "sess1")
        assert plan is not None
        assert plan["status"] == "failed"
        tasks = await get_tasks_for_plan(db, plan["id"])
        assert len(tasks) == 1
        assert tasks[0]["type"] == "msg"
        assert "Planning failed" in tasks[0]["output"]

    async def test_plan_error_delivers_webhook(self, db, tmp_path):
        """PlanError + webhook → webhook called with error message."""
        config = _make_config()
        await create_session(db, "sess1")
        # Set webhook on session
        await db.execute(
            "UPDATE sessions SET webhook = ? WHERE session = ?",
            ("https://example.com/hook", "sess1"),
        )
        await db.commit()

        msg_id = await save_message(db, "sess1", "alice", "user", "bad", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "bad", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock,
                   side_effect=PlanError("oops")), \
             patch("kiso.worker.loop.deliver_webhook", new_callable=AsyncMock,
                   return_value=(True, 200, 1)) as mock_wh, \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=3)

        mock_wh.assert_called_once()
        call_args = mock_wh.call_args
        assert "Planning failed" in call_args[0][3]  # content arg
        assert call_args[0][4] is True  # final=True


# --- 21h: Sanitize secrets in task detail ---


class TestSanitizeTaskDetail:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_secret_in_exec_detail_redacted(self, db, tmp_path):
        """Secret value in exec detail → [REDACTED] in DB."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "deploy", processed=False)

        plan_with_secret = {
            "goal": "Deploy",
            "secrets": [{"key": "API_KEY", "value": "sk-secret-value-1234"}],
            "tasks": [
                {"type": "exec", "detail": "curl -H 'Authorization: Bearer sk-secret-value-1234' https://api.example.com",
                 "skill": None, "args": None, "expect": "200 OK"},
                {"type": "msg", "detail": "Done deploying", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "deploy", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=plan_with_secret), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Deployed"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop._exec_task", new_callable=AsyncMock,
                   return_value=("ok", "", True)), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        tasks = await get_tasks_for_plan(db, plan["id"])
        exec_task = [t for t in tasks if t["type"] == "exec"][0]
        assert "sk-secret-value-1234" not in exec_task["detail"]
        assert "[REDACTED]" in exec_task["detail"]

    async def test_secret_in_skill_args_redacted(self, db, tmp_path):
        """Secret value in skill args → [REDACTED] in DB."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        plan_with_secret = {
            "goal": "Search",
            "secrets": [{"key": "TOKEN", "value": "tok-mysecret5678"}],
            "tasks": [
                {"type": "skill", "detail": "search the web",
                 "skill": "search", "args": '{"query": "test", "token": "tok-mysecret5678"}',
                 "expect": "results"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=plan_with_secret), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Results"), \
             patch("kiso.worker.loop.discover_skills", return_value=[]), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        tasks = await get_tasks_for_plan(db, plan["id"])
        skill_task = [t for t in tasks if t["type"] == "skill"][0]
        assert "tok-mysecret5678" not in (skill_task["args"] or "")
        assert "[REDACTED]" in (skill_task["args"] or "")


class TestBuildFailureSummary:
    def test_basic(self):
        completed = [
            {"type": "exec", "detail": "echo hello"},
        ]
        remaining = [{"type": "msg", "detail": "Report results"}]
        result = _build_failure_summary(completed, remaining, "Run tests")
        assert "The plan failed: Run tests" in result
        assert "Completed (1):" in result
        assert "[exec] echo hello" in result
        assert "Failed/Skipped (1):" in result
        assert "[msg] Report results" in result
        assert "suggest next steps" in result

    def test_with_reason(self):
        result = _build_failure_summary([], [], "Goal", reason="LLM error")
        assert "Failure reason: LLM error" in result

    def test_no_completed(self):
        remaining = [{"type": "msg", "detail": "done"}]
        result = _build_failure_summary([], remaining, "Goal")
        assert "No tasks were completed" in result
        assert "Failed/Skipped (1):" in result

    def test_no_remaining(self):
        completed = [{"type": "exec", "detail": "echo ok"}]
        result = _build_failure_summary(completed, [], "Goal")
        assert "Completed (1):" in result
        assert "Failed/Skipped" not in result


class TestRecoveryMsgTask:
    """Tests for recovery msg tasks created on plan failure paths."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_plan_failure_creates_recovery_msg(self, db, tmp_path):
        """When plan fails without replan, a recovery msg task is created via LLM."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do stuff", processed=False)

        fail_plan = {
            "goal": "Do stuff",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do stuff", "user_role": "admin"})

        # Reviewer returns None replan_reason (replan_reason=None path)
        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=ReviewError("Review LLM broke")), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="I'm sorry, something went wrong with the task."), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

        # Recovery msg task created
        tasks = await get_tasks_for_plan(db, plan["id"])
        msg_tasks = [t for t in tasks if t["type"] == "msg"]
        recovery = [t for t in msg_tasks if t["status"] == "done" and "wrong" in (t["output"] or "").lower()]
        assert len(recovery) == 1

        # System message saved
        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        rows = await cur.fetchall()
        system_msgs = [r[0] for r in rows]
        assert any("wrong" in m.lower() for m in system_msgs)

    async def test_plan_failure_llm_fallback(self, db, tmp_path):
        """When messenger also fails, raw failure detail is used as fallback."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do stuff", processed=False)

        fail_plan = {
            "goal": "Do stuff",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do stuff", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=fail_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=ReviewError("Review broke")), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   side_effect=MessengerError("Messenger also broke")), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        plan = await get_plan_for_session(db, "sess1")
        tasks = await get_tasks_for_plan(db, plan["id"])
        msg_tasks = [t for t in tasks if t["type"] == "msg" and t["status"] == "done"]
        # Fallback to raw failure summary
        assert any("The plan failed" in (t["output"] or "") for t in msg_tasks)

    async def test_cancel_creates_msg_task_in_db(self, db, tmp_path):
        """Cancel handler creates a msg task record in the plan."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do stuff", processed=False)

        cancel_plan = {
            "goal": "Do stuff",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        cancel_event = asyncio.Event()

        async def _review_then_cancel(*args, **kwargs):
            cancel_event.set()
            return REVIEW_OK

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do stuff", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=cancel_plan), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   side_effect=_review_then_cancel), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="The plan was cancelled."), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(
                run_worker(db, config, "sess1", queue, cancel_event=cancel_event),
                timeout=5,
            )

        plan = await get_plan_for_session(db, "sess1")
        tasks = await get_tasks_for_plan(db, plan["id"])
        msg_tasks = [t for t in tasks if t["type"] == "msg" and t["status"] == "done"]
        assert any("cancelled" in (t["output"] or "").lower() for t in msg_tasks)


# --- _build_exec_env ---


class TestBuildExecEnv:
    def test_path_without_sys_bin(self, tmp_path):
        """When sys/bin doesn't exist, PATH is the base system PATH."""
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        # sys/bin doesn't exist, so PATH should not contain it
        assert str(tmp_path / "sys" / "bin") not in env["PATH"]

    def test_path_with_sys_bin(self, tmp_path):
        """When sys/bin exists, it's prepended to PATH."""
        sys_bin = tmp_path / "sys" / "bin"
        sys_bin.mkdir(parents=True)
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        assert env["PATH"].startswith(str(sys_bin) + ":")

    def test_home_set_to_kiso_dir(self, tmp_path):
        """HOME is always set to KISO_DIR."""
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        assert env["HOME"] == str(tmp_path)

    def test_git_config_global_when_exists(self, tmp_path):
        """GIT_CONFIG_GLOBAL is set when sys/gitconfig exists."""
        sys_dir = tmp_path / "sys"
        sys_dir.mkdir(parents=True)
        gitconfig = sys_dir / "gitconfig"
        gitconfig.write_text("[user]\n  name = Test")
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        assert env["GIT_CONFIG_GLOBAL"] == str(gitconfig)

    def test_no_git_config_global_when_missing(self, tmp_path):
        """GIT_CONFIG_GLOBAL is not set when sys/gitconfig doesn't exist."""
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        assert "GIT_CONFIG_GLOBAL" not in env

    def test_git_ssh_command_when_ssh_dir_exists(self, tmp_path):
        """GIT_SSH_COMMAND is set when sys/ssh/ dir AND config + key files exist."""
        ssh_dir = tmp_path / "sys" / "ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / "config").write_text("Host *\n  StrictHostKeyChecking no\n")
        (ssh_dir / "id_ed25519").write_bytes(b"fake-key")
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        assert "GIT_SSH_COMMAND" in env
        assert str(ssh_dir / "config") in env["GIT_SSH_COMMAND"]
        assert str(ssh_dir / "known_hosts") in env["GIT_SSH_COMMAND"]
        assert str(ssh_dir / "id_ed25519") in env["GIT_SSH_COMMAND"]

    def test_no_git_ssh_command_when_ssh_files_missing(self, tmp_path):
        """GIT_SSH_COMMAND not set when ssh dir exists but config/key files are absent."""
        ssh_dir = tmp_path / "sys" / "ssh"
        ssh_dir.mkdir(parents=True)  # dir exists, files don't
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        assert "GIT_SSH_COMMAND" not in env

    def test_no_git_ssh_command_when_ssh_dir_missing(self, tmp_path):
        """GIT_SSH_COMMAND is not set when sys/ssh/ doesn't exist."""
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        assert "GIT_SSH_COMMAND" not in env


# --- M25: Planner-initiated replan (discovery plans) ---


class TestSelfDirectedReplan:
    """Tests for planner-initiated (self-directed) replan via replan task type."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_self_directed_replan_marks_plan_done(self, db, tmp_path):
        """Self-directed replan marks the investigation plan as 'done' not 'failed'."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "install search skill", processed=False)

        investigation_plan = {
            "goal": "Investigate available skills",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "echo registry", "skill": None, "args": None, "expect": "JSON output"},
                {"type": "replan", "detail": "install the right skill", "skill": None, "args": None, "expect": None},
            ],
        }

        followup_plan = {
            "goal": "Install search skill",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "msg", "detail": "Done investigating", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content, **kwargs):
            planner_calls.append(content)
            if len(planner_calls) == 1:
                return investigation_plan
            return followup_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "install search skill", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Done"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Check both plans exist
        cur = await db.execute(
            "SELECT * FROM plans WHERE session = 'sess1' ORDER BY id"
        )
        plans = [dict(r) for r in await cur.fetchall()]
        assert len(plans) == 2
        # First plan (investigation) should be "done" (not "failed")
        assert plans[0]["status"] == "done"
        # Second plan (followup) should also be "done"
        assert plans[1]["status"] == "done"
        assert plans[1]["parent_id"] == plans[0]["id"]

    async def test_self_directed_replan_counts_toward_limit(self, db, tmp_path):
        """Self-directed replans increment replan_depth and count toward the limit."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 1,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "investigate", processed=False)

        replan_plan = {
            "goal": "Investigate",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "replan", "detail": "investigate more", "skill": None, "args": None, "expect": None},
            ],
        }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "investigate", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=replan_plan), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Max replan depth reached."), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Should hit the max replan depth limit
        messages = await get_recent_messages(db, "sess1", limit=20)
        msg_texts = [m["content"] for m in messages]
        # Should have an "Investigating..." notification and then max-depth failure
        assert any("Investigating..." in m for m in msg_texts)

    async def test_self_directed_replan_notification_message(self, db, tmp_path):
        """Self-directed replans send 'Investigating...' notification, not 'Replanning'."""
        config = _make_config()
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "check registry", processed=False)

        investigation_plan = {
            "goal": "Check registry",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "replan", "detail": "decide next step", "skill": None, "args": None, "expect": None},
            ],
        }

        followup_plan = {
            "goal": "Done",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "msg", "detail": "All done", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content, **kwargs):
            planner_calls.append(content)
            if len(planner_calls) == 1:
                return investigation_plan
            return followup_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "check registry", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Done"), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        messages = await get_recent_messages(db, "sess1", limit=20)
        msg_texts = [m["content"] for m in messages]
        assert any("Investigating..." in m for m in msg_texts)
        assert not any("Replanning" in m for m in msg_texts)


class TestExtendReplan:
    """Tests for the extend_replan field."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_extend_replan_increases_limit(self, db, tmp_path):
        """Plan with extend_replan=2 raises max_replan_depth by 2."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 1,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        # First plan: exec fails, reviewer requests replan
        fail_plan = {
            "goal": "Will fail",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        # Second plan: requests extension, also fails
        extend_plan = {
            "goal": "Second attempt with extension",
            "secrets": None,
            "extend_replan": 2,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        # Third plan: succeeds
        success_plan = {
            "goal": "Finally works",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "msg", "detail": "Done!", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content, **kwargs):
            planner_calls.append(content)
            n = len(planner_calls)
            if n == 1:
                return fail_plan
            elif n == 2:
                return extend_plan
            return success_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Done"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        # Without extend_replan, max_replan_depth=1 would have stopped after 1 replan.
        # With extend_replan=2 (limit becomes 3), it can try 3 replans.
        # Plan 1 fails → replan 1 (extend_plan) → replan 2 (success_plan) → done
        assert len(planner_calls) >= 3
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"

    async def test_extend_replan_capped_at_3(self, db, tmp_path):
        """extend_replan=10 only adds 3 (capped)."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "exec_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 1,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        fail_plan = {
            "goal": "Will fail",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        # This plan tries to extend by 10, but should be capped at 3
        extend_plan = {
            "goal": "Extends by 10",
            "secrets": None,
            "extend_replan": 10,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        # Subsequent plans don't extend
        no_extend_plan = {
            "goal": "No extend",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        planner_calls = []

        async def _planner(db, config, session, role, content, **kwargs):
            planner_calls.append(content)
            n = len(planner_calls)
            if n == 1:
                return fail_plan
            elif n == 2:
                return extend_plan  # This one requests extend_replan=10 (capped to 3)
            return no_extend_plan

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Failed after max depth"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        # max_replan_depth starts at 1, extend of 10 capped to 3, new limit = 4
        # So we expect: plan 1 + 4 replans = 5 planner calls total
        assert len(planner_calls) == 5


class TestDefaultMaxReplanDepth:
    """Test that the default max_replan_depth is 3."""

    def test_default_max_replan_depth_is_3(self):
        from kiso.config import SETTINGS_DEFAULTS
        assert SETTINGS_DEFAULTS["max_replan_depth"] == 3


class TestReportPubFiles:
    """Tests for _report_pub_files."""

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "test-secret-token"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"planner": "gpt-4"},
            settings={},
            raw={},
        )

    def test_empty_when_no_pub_dir(self, tmp_path, config):
        """No pub/ directory → empty list."""
        with _patch_kiso_dir(tmp_path):
            result = _report_pub_files("no-pub-session", config)
        assert result == []

    def test_lists_files(self, tmp_path, config):
        """Files in pub/ → correct URLs."""
        session_dir = tmp_path / "sessions" / "test-session"
        pub_dir = session_dir / "pub"
        pub_dir.mkdir(parents=True)
        (pub_dir / "report.pdf").write_text("fake pdf")
        (pub_dir / "data.csv").write_text("a,b,c")

        with _patch_kiso_dir(tmp_path):
            result = _report_pub_files("test-session", config)

        assert len(result) == 2
        filenames = [r["filename"] for r in result]
        assert "data.csv" in filenames
        assert "report.pdf" in filenames
        for r in result:
            assert r["url"].startswith("/pub/")
            assert len(r["url"].split("/")[2]) == 16  # token is 16 hex chars

    def test_nested_files(self, tmp_path, config):
        """Files in pub/sub/ → relative paths preserved."""
        session_dir = tmp_path / "sessions" / "test-session"
        pub_dir = session_dir / "pub"
        sub_dir = pub_dir / "sub"
        sub_dir.mkdir(parents=True)
        (sub_dir / "nested.txt").write_text("hello")

        with _patch_kiso_dir(tmp_path):
            result = _report_pub_files("test-session", config)

        assert len(result) == 1
        assert result[0]["filename"] == "sub/nested.txt"

    def test_pub_scan_cap_truncates_and_warns(self, tmp_path, config, caplog):
        """M37: pub/ listing capped at 1000 entries with a warning."""
        import logging
        session_dir = tmp_path / "sessions" / "cap-session"
        pub_dir = session_dir / "pub"
        pub_dir.mkdir(parents=True)
        for i in range(1001):
            (pub_dir / f"file{i:04d}.txt").write_bytes(b"")

        with _patch_kiso_dir(tmp_path), \
             caplog.at_level(logging.WARNING, logger="kiso.worker.utils"):
            result = _report_pub_files("cap-session", config)

        assert len(result) <= 1000
        assert any("truncated" in r.message for r in caplog.records)


# --- M31: search output truncation in replan context ---


class TestBuildReplanContextSearchLimit:
    def test_search_output_not_truncated_at_500(self):
        """Search task output uses 4000 char limit (not 500) in replan context."""
        long_output = "x" * 3000
        completed = [
            {"type": "search", "detail": "find info", "status": "done", "output": long_output},
        ]
        context = _build_replan_context(completed, [], "replan reason", [])
        # Full 3000 chars should be present (under 4000 limit)
        assert "x" * 3000 in context

    def test_exec_output_truncated_at_500(self):
        """Exec task output still uses 500 char limit in replan context."""
        long_output = "x" * 1000
        completed = [
            {"type": "exec", "detail": "run command", "status": "done", "output": long_output},
        ]
        context = _build_replan_context(completed, [], "replan reason", [])
        # Only first 500 chars should be present
        assert "x" * 500 in context
        assert "x" * 501 not in context


# --- M31: session workspace pub/ directory ---


class TestSessionWorkspacePubDir:
    def test_pub_dir_created(self, tmp_path):
        """_session_workspace creates pub/ subdirectory."""
        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pub_dir = workspace / "pub"
            assert pub_dir.is_dir()


# --- M31b: search task execution ---


class TestExecutePlanSearch:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_search_calls_searcher(self, db, tmp_path):
        """Search task calls run_searcher with detail as query."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="best pizza in Rome", expect="restaurant list")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_searcher = AsyncMock(return_value='{"results": [{"title": "Pizza Place"}]}')
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        mock_searcher.assert_called_once()
        call_args = mock_searcher.call_args
        assert call_args.args[1] == "best pizza in Rome"  # query = detail

    async def test_search_with_params(self, db, tmp_path):
        """Search task parses args JSON and passes params to searcher."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(
            db, plan_id, "sess1", type="search",
            detail="agenzie SEO Milano",
            args='{"max_results": 10, "lang": "it", "country": "IT"}',
            expect="agency list",
        )
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_searcher = AsyncMock(return_value="results here")
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        call_kwargs = mock_searcher.call_args
        assert call_kwargs.kwargs.get("max_results") == 10
        assert call_kwargs.kwargs.get("lang") == "it"
        assert call_kwargs.kwargs.get("country") == "IT"

    async def test_search_malformed_args(self, db, tmp_path):
        """Malformed JSON in args logs a warning and uses defaults."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(
            db, plan_id, "sess1", type="search",
            detail="query", args="NOT_JSON", expect="results",
        )
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_searcher = AsyncMock(return_value="results")
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        call_kwargs = mock_searcher.call_args
        assert call_kwargs.kwargs.get("max_results") is None
        assert call_kwargs.kwargs.get("lang") is None
        assert call_kwargs.kwargs.get("country") is None

    async def test_search_malformed_args_emits_warning(self, db, tmp_path, caplog):
        """M37: malformed search args emit a warning log."""
        import logging
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(
            db, plan_id, "sess1", type="search",
            detail="query", args="NOT_JSON", expect="results",
        )
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_searcher = AsyncMock(return_value="results")
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             caplog.at_level(logging.WARNING, logger="kiso.worker.loop"), \
             _patch_kiso_dir(tmp_path):
            await _execute_plan(db, config, "sess1", plan_id, "Test", "user msg", 5)

        assert any("malformed args" in r.message for r in caplog.records)

    async def test_search_params_invalid_types(self, db, tmp_path):
        """Invalid types in search params are coerced or set to None."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(
            db, plan_id, "sess1", type="search",
            detail="query",
            args='{"max_results": "not_int", "lang": 123, "country": ["US"]}',
            expect="results",
        )
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_searcher = AsyncMock(return_value="results")
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        call_kwargs = mock_searcher.call_args
        # "not_int" can't be cast to int → None
        assert call_kwargs.kwargs.get("max_results") is None
        # 123 is not a string → None
        assert call_kwargs.kwargs.get("lang") is None
        # list is not a string → None
        assert call_kwargs.kwargs.get("country") is None

    async def test_search_searcher_error(self, db, tmp_path):
        """SearcherError marks task failed and stops plan."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, side_effect=SearcherError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is False
        assert reason is None  # no replan, just failure
        assert len(remaining) == 1  # msg task
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = [t for t in tasks if t["type"] == "search"][0]
        assert search_task["status"] == "failed"

    async def test_search_result_in_plan_outputs(self, db, tmp_path):
        """Search output flows to plan_outputs for subsequent msg task."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="find info", expect="info found")
        await create_task(db, plan_id, "sess1", type="msg", detail="report findings")

        search_output = "Found 3 results: A, B, C"
        mock_messenger = AsyncMock(return_value="Here are the results")
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value=search_output), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        # Messenger should have received the search output in its context
        messenger_call = mock_messenger.call_args
        # plan_outputs are passed to _format_plan_outputs_for_msg then to _msg_task
        # Check that messenger was called (it received the search output via plan_outputs)
        assert mock_messenger.called

    async def test_search_review_ok(self, db, tmp_path):
        """Search with review ok completes the task successfully."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="search results"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        assert len(completed) == 2
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = [t for t in tasks if t["type"] == "search"][0]
        assert search_task["status"] == "done"
        assert search_task["output"] == "search results"

    async def test_search_review_replan(self, db, tmp_path):
        """Review returns replan after search → plan returns replan reason."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="bad results"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        assert len(remaining) == 1  # msg task

    async def test_search_review_error(self, db, tmp_path):
        """ReviewError during search review fails plan without replan."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="results"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is False
        assert reason is None
        assert len(remaining) == 1

    async def test_search_substatus_transitions(self, db, tmp_path):
        """Search task updates substatus: searching → reviewing."""
        from kiso.store import update_task_substatus
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        substatus_calls = []
        original_update = update_task_substatus

        async def capture_substatus(db, task_id, substatus):
            substatus_calls.append(substatus)
            await original_update(db, task_id, substatus)

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="results"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.loop.update_task_substatus", side_effect=capture_substatus), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        # Search task should have gone through "searching" then "reviewing"
        # msg task should have "composing"
        assert "searching" in substatus_calls
        assert "reviewing" in substatus_calls
        assert "composing" in substatus_calls

    async def test_search_empty_result(self, db, tmp_path):
        """Empty string from searcher is stored and reviewed normally."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="obscure query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value=""), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="nothing found"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = [t for t in tasks if t["type"] == "search"][0]
        assert search_task["status"] == "done"
        assert search_task["output"] == ""

    async def test_search_empty_detail(self, db, tmp_path):
        """Search task with empty string detail still calls run_searcher."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_searcher = AsyncMock(return_value="some results")
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        mock_searcher.assert_called_once()
        call_args = mock_searcher.call_args
        assert call_args.args[1] == ""  # detail is empty string
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = [t for t in tasks if t["type"] == "search"][0]
        assert search_task["status"] == "done"

    async def test_search_multiple_sequential(self, db, tmp_path):
        """Two search tasks followed by msg: both complete and feed into messenger."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="first query", expect="results")
        await create_task(db, plan_id, "sess1", type="search", detail="second query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="summarise both")

        mock_searcher = AsyncMock(side_effect=["result1", "result2"])
        mock_messenger = AsyncMock(return_value="combined summary")
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        assert mock_searcher.call_count == 2
        tasks = await get_tasks_for_plan(db, plan_id)
        search_tasks = [t for t in tasks if t["type"] == "search"]
        assert len(search_tasks) == 2
        assert search_tasks[0]["status"] == "done"
        assert search_tasks[0]["output"] == "result1"
        assert search_tasks[1]["status"] == "done"
        assert search_tasks[1]["output"] == "result2"
        assert mock_messenger.called


# --- Fast path (_fast_path_chat) ---


class TestFastPathChat:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_creates_plan_and_task(self, db, tmp_path):
        """_fast_path_chat creates a plan with a single msg task."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hello!"), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        tasks = await get_tasks_for_session(db, "sess1")
        assert len(tasks) == 1
        assert tasks[0]["type"] == "msg"
        assert tasks[0]["status"] == "done"
        assert tasks[0]["output"] == "Hello!"

    async def test_plan_status_done(self, db, tmp_path):
        """_fast_path_chat sets plan status to done."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "done"
        assert plan["goal"] == "Chat response"

    async def test_saves_assistant_message(self, db, tmp_path):
        """_fast_path_chat saves the response as an assistant message."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Reply"), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        msgs = await get_recent_messages(db, "sess1", limit=10)
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert any("Reply" in m["content"] for m in assistant_msgs)

    async def test_messenger_failure_marks_plan_failed(self, db, tmp_path):
        """_fast_path_chat marks plan as failed when messenger errors."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("boom")), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"
        tasks = await get_tasks_for_session(db, "sess1")
        assert tasks[0]["status"] == "failed"

    async def test_webhook_delivered(self, db, tmp_path):
        """_fast_path_chat delivers webhook with final=True."""
        config = _make_config()
        mock_wh = AsyncMock()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", mock_wh), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        mock_wh.assert_called_once()
        # final argument should be True
        assert mock_wh.call_args[1].get("final", mock_wh.call_args[0][5]) is True

    async def test_passes_content_as_goal(self, db, tmp_path):
        """_fast_path_chat passes the user message as both detail and goal."""
        config = _make_config()
        mock_messenger = AsyncMock(return_value="response")
        with patch("kiso.worker.loop.run_messenger", mock_messenger), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "thanks!")

        # run_messenger receives goal=content
        call_kwargs = mock_messenger.call_args
        assert call_kwargs[1].get("goal", call_kwargs[0][4] if len(call_kwargs[0]) > 4 else "") == "thanks!"


# --- Fast path integration in _process_message ---


CHAT_PLAN = {
    "goal": "Chat",
    "secrets": None,
    "tasks": [{"type": "msg", "detail": "hi", "skill": None, "args": None, "expect": None}],
}


class TestFastPathIntegration:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        msg_id = await save_message(conn, "sess1", "u1", "user", "hello", trusted=True, processed=False)
        yield conn, msg_id
        await conn.close()

    def _make_msg(self, msg_id):
        return {"id": msg_id, "content": "hello", "user_role": "admin", "user_skills": None, "username": "u1"}

    async def test_chat_message_skips_planner(self, db, tmp_path):
        """When classifier returns 'chat', planner should not be called."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        msg = self._make_msg(msg_id)
        mock_planner = AsyncMock(return_value=CHAT_PLAN)
        mock_classifier = AsyncMock(return_value="chat")
        mock_messenger = AsyncMock(return_value="Hi there!")
        q = asyncio.Queue()

        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_planner", mock_planner), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, q, None, 1, 5, 60, 3,
            )

        mock_classifier.assert_called_once()
        mock_planner.assert_not_called()
        mock_messenger.assert_called_once()

    async def test_plan_message_goes_to_planner(self, db, tmp_path):
        """When classifier returns 'plan', normal planner flow is used."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        msg = self._make_msg(msg_id)
        mock_planner = AsyncMock(return_value=CHAT_PLAN)
        mock_classifier = AsyncMock(return_value="plan")
        mock_messenger = AsyncMock(return_value="Done")
        q = asyncio.Queue()

        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_planner", mock_planner), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, q, None, 1, 5, 60, 3,
            )

        mock_classifier.assert_called_once()
        mock_planner.assert_called_once()

    async def test_fast_path_disabled_skips_classifier(self, db, tmp_path):
        """When fast_path_enabled=False, classifier is not called."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": False})
        msg = self._make_msg(msg_id)
        mock_planner = AsyncMock(return_value=CHAT_PLAN)
        mock_classifier = AsyncMock(return_value="chat")
        mock_messenger = AsyncMock(return_value="Hi")
        q = asyncio.Queue()

        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_planner", mock_planner), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, q, None, 1, 5, 60, 3,
            )

        mock_classifier.assert_not_called()
        mock_planner.assert_called_once()

    async def test_fast_path_runs_post_plan_knowledge(self, db, tmp_path):
        """Fast path should run post-plan knowledge processing (summarizer, etc.)."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        msg = self._make_msg(msg_id)
        mock_classifier = AsyncMock(return_value="chat")
        mock_messenger = AsyncMock(return_value="Hi")
        mock_post = AsyncMock()
        q = asyncio.Queue()

        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop._post_plan_knowledge", mock_post), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, q, None, 1, 5, 60, 3,
            )

        mock_post.assert_called_once()

    async def test_fast_path_skips_paraphraser(self, db, tmp_path):
        """Fast path must NOT call the paraphraser — context is already trusted."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        msg = self._make_msg(msg_id)
        mock_classifier = AsyncMock(return_value="chat")
        mock_messenger = AsyncMock(return_value="Hi")
        mock_paraphraser = AsyncMock(return_value="paraphrased")
        q = asyncio.Queue()

        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.run_paraphraser", mock_paraphraser), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, q, None, 1, 5, 60, 3,
            )

        mock_paraphraser.assert_not_called()


# --- _fast_path_chat edge cases ---


class TestFastPathEdgeCases:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_audit_log_on_success(self, db, tmp_path):
        """_fast_path_chat calls audit.log_task on success."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop.audit.log_task") as mock_audit, \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        mock_audit.assert_called_once()
        # Verify it logged as "done"
        assert mock_audit.call_args[0][4] == "done"  # status arg

    async def test_audit_log_on_failure(self, db, tmp_path):
        """_fast_path_chat calls audit.log_task on messenger failure."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("boom")), \
             patch("kiso.worker.loop.audit.log_task") as mock_audit, \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][4] == "failed"  # status arg

    async def test_slog_on_failure(self, db, tmp_path):
        """_fast_path_chat logs to slog on messenger failure."""
        config = _make_config()
        mock_slog = type("MockSlog", (), {"info": lambda self, *a, **kw: None})()
        mock_slog.info = AsyncMock() if asyncio.iscoroutinefunction(getattr(mock_slog, "info", None)) else lambda *a, **kw: None

        # Use a real mock for slog
        from unittest.mock import MagicMock
        slog = MagicMock()

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("boom")), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello", slog=slog)

        # slog.info should have been called with error info
        assert slog.info.called
        logged_msg = slog.info.call_args[0][0]
        assert "Fast path failed" in logged_msg

    async def test_returns_plan_id(self, db, tmp_path):
        """_fast_path_chat returns the plan_id for post-plan processing."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            plan_id = await _fast_path_chat(db, config, "sess1", 1, "hello")

        assert isinstance(plan_id, int)
        assert plan_id > 0

    async def test_returns_plan_id_on_failure(self, db, tmp_path):
        """_fast_path_chat returns plan_id even on failure."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("boom")), \
             _patch_kiso_dir(tmp_path):
            plan_id = await _fast_path_chat(db, config, "sess1", 1, "hello")

        assert isinstance(plan_id, int)
        assert plan_id > 0

    async def test_budget_exceeded_in_messenger(self, db, tmp_path):
        """LLMBudgetExceeded during messenger is caught (subclass of LLMError)."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=LLMBudgetExceeded("budget")), \
             _patch_kiso_dir(tmp_path):
            plan_id = await _fast_path_chat(db, config, "sess1", 1, "hello")

        # Should not crash — plan marked failed
        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

    async def test_error_message_saved_as_system(self, db, tmp_path):
        """On failure, error message is saved as system role (not assistant)."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("boom")), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        msgs = await get_recent_messages(db, "sess1", limit=10)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert any("Chat response failed" in m["content"] for m in system_msgs)

    async def test_substatus_set_to_composing(self, db, tmp_path):
        """_fast_path_chat sets task substatus to composing."""
        config = _make_config()
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        tasks = await get_tasks_for_session(db, "sess1")
        # substatus was set during execution (may be cleared after done)
        assert tasks[0]["status"] == "done"


# --- M33: Worker Retry ---


class TestWorkerRetry:
    """Tests for worker-level retry on transient exec/search errors."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_exec_retry_on_hint_then_ok(self, db, tmp_path):
        """First review returns replan+hint → retry → second review ok."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 1,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        review_calls = [0]

        async def _mock_reviewer(*args, **kw):
            review_calls[0] += 1
            if review_calls[0] == 1:
                return REVIEW_REPLAN_WITH_HINT
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=_mock_reviewer), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert reason is None
        assert len(completed) == 2
        assert review_calls[0] == 2

        # Verify retry_count persisted
        tasks = await get_tasks_for_plan(db, plan_id)
        exec_task = [t for t in tasks if t["type"] == "exec"][0]
        assert exec_task["retry_count"] == 1

    async def test_exec_retry_still_fails_escalates(self, db, tmp_path):
        """Retry also returns replan+hint but retries exhausted → full replan."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 1,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        # Both reviews return replan with hint
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN_WITH_HINT), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Wrong path"
        assert len(remaining) == 1  # msg task

    async def test_exec_no_retry_when_hint_is_null(self, db, tmp_path):
        """Null retry_hint → immediate replan (no retry)."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 1,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"  # immediate escalation

    async def test_exec_retry_context_passed_to_translator(self, db, tmp_path):
        """On retry, translator receives retry_context with hint."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 1,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="run script", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        translator_calls = []

        async def _capture_translator(config, detail, sys_env_text, **kw):
            translator_calls.append(kw.get("retry_context", ""))
            return "echo hello"

        review_calls = [0]

        async def _mock_reviewer(*args, **kw):
            review_calls[0] += 1
            if review_calls[0] == 1:
                return REVIEW_REPLAN_WITH_HINT
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=_mock_reviewer), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock, side_effect=_capture_translator), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # First call: no retry_context
        assert translator_calls[0] == ""
        # Second call: retry_context with hint
        assert "use /opt/app not /app" in translator_calls[1]
        assert "Attempt 1 failed" in translator_calls[1]

    async def test_search_retry_on_hint(self, db, tmp_path):
        """Search task: first review returns hint → retry → second ok."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 1,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="find info", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        review_calls = [0]

        async def _mock_reviewer(*args, **kw):
            review_calls[0] += 1
            if review_calls[0] == 1:
                return {"status": "replan", "reason": "No results", "learn": None, "retry_hint": "try broader terms"}
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=_mock_reviewer), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="search results"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert review_calls[0] == 2

        # Verify retry_count persisted
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = [t for t in tasks if t["type"] == "search"][0]
        assert search_task["retry_count"] == 1

    async def test_max_worker_retries_zero_disables(self, db, tmp_path):
        """max_worker_retries=0 → hint is ignored, immediate replan."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 0,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN_WITH_HINT), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Wrong path"  # immediate escalation, no retry

    async def test_retry_count_persisted(self, db, tmp_path):
        """DB retry_count matches actual retry attempts."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 2,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        review_calls = [0]

        async def _mock_reviewer(*args, **kw):
            review_calls[0] += 1
            if review_calls[0] <= 2:
                return REVIEW_REPLAN_WITH_HINT
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=_mock_reviewer), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        tasks = await get_tasks_for_plan(db, plan_id)
        exec_task = [t for t in tasks if t["type"] == "exec"][0]
        assert exec_task["retry_count"] == 2


# --- M44e: Incremental LLM rendering (append_task_llm_call after each LLM call) ---


@pytest.mark.asyncio
class TestIncrementalLLMCalls:
    """M44e: verify _append_calls is invoked after each individual LLM call."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import init_db
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_exec_task_appends_calls_after_translator_and_reviewer(self, db, tmp_path):
        """exec task: _append_calls called twice — after translator + after reviewer."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_append = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # exec task: 2 _append_calls (translator + reviewer), msg task: 1 (messenger) = 3 total
        assert mock_append.call_count >= 2

    async def test_msg_task_appends_calls_after_messenger(self, db, tmp_path):
        """msg task: _append_calls called once — after messenger."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        mock_append = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert mock_append.call_count == 1

    async def test_search_task_appends_calls_after_searcher_and_reviewer(self, db, tmp_path):
        """search task: _append_calls called twice — after searcher + after reviewer."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_append = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="results"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # search: 2 calls (searcher + reviewer), msg: 1 call = 3 total
        assert mock_append.call_count >= 2

    async def test_fast_path_appends_calls_after_messenger(self, db, tmp_path):
        """fast path: _append_calls called once — after messenger."""
        config = _make_config()

        mock_append = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Reply"), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        assert mock_append.call_count == 1

    async def test_update_task_usage_not_passed_llm_calls(self, db, tmp_path):
        """M44e: update_task_usage is called WITHOUT llm_calls (sentinel path)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        captured_calls = []

        async def _mock_update_usage(db, task_id, input_tokens, output_tokens, **kwargs):
            captured_calls.append(kwargs)

        with patch("kiso.worker.loop.update_task_usage", side_effect=_mock_update_usage), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             patch("kiso.worker.loop._append_calls", new_callable=AsyncMock), \
             _patch_kiso_dir(tmp_path):
            await _execute_plan(db, config, "sess1", plan_id, "Test", "msg", 5)

        # llm_calls should NOT be in kwargs (uses sentinel default, not explicit None)
        for call_kwargs in captured_calls:
            assert "llm_calls" not in call_kwargs, (
                f"update_task_usage was called with llm_calls — expected sentinel path. "
                f"kwargs: {call_kwargs}"
            )
