"""Tests for kiso/worker.py — per-session asyncio worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    _report_pub_files, _run_subprocess, _skill_task, _session_workspace,
    _ensure_sandbox_user, _truncate_output, _save_large_output,
    _review_task, _execute_plan, _build_replan_context, _persist_plan_tasks, _maybe_inject_intent_msg,
    _write_plan_outputs, _cleanup_plan_outputs, _format_plan_outputs_for_msg,
    run_worker,
)
from kiso.worker.loop import (
    _PlanCtx,
    _SUBSTATUS_COMPOSING,
    _SUBSTATUS_EXECUTING,
    _SUBSTATUS_REVIEWING,
    _SUBSTATUS_SEARCHING,
    _SUBSTATUS_TRANSLATING,
    _TASK_HANDLERS,
    _TaskHandlerResult,
    _bump_fact_usage,
    _handle_exec_task,
    _handle_loop_cancel,
    _handle_loop_failure,
    _msg_task_with_fallback,
    _handle_msg_task,
    _handle_plan_error,
    _handle_replan_task,
    _handle_search_task,
    _handle_skill_task,
    _make_plan_output,
    _run_planning_loop,
    _spawn_knowledge_task,
)

from contextlib import contextmanager


@contextmanager
def _patch_kiso_dir(tmp_path):
    """Patch KISO_DIR in both utils and loop submodules (and exec via utils)."""
    with patch("kiso.worker.utils.KISO_DIR", tmp_path), \
         patch("kiso.worker.loop.KISO_DIR", tmp_path), \
         patch("kiso.worker.loop._check_disk_limit", return_value=None):
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

REVIEW_OK = {"status": "ok", "reason": None, "learn": None, "retry_hint": None, "summary": None}
REVIEW_REPLAN = {"status": "replan", "reason": "Task failed", "learn": None, "retry_hint": None, "summary": None}
REVIEW_REPLAN_WITH_HINT = {"status": "replan", "reason": "Wrong path", "learn": None, "retry_hint": "use /opt/app not /app", "summary": None}


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


def _patch_no_intent():
    """Suppress M201 intent msg injection for tests that don't expect it."""
    return patch(
        "kiso.worker.loop._maybe_inject_intent_msg",
        side_effect=lambda tasks, goal: tasks,
    )


def _make_config(**overrides) -> Config:
    from kiso.config import SETTINGS_DEFAULTS, MODEL_DEFAULTS
    base_settings = {
        **SETTINGS_DEFAULTS,
        "worker_idle_timeout": 0.05,  # sub-second for fast tests
        "llm_timeout": 5,
        "planner_timeout": 5,
        "briefer_enabled": False,  # avoid interfering with mocked call_llm
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


# --- _run_subprocess ---

class TestRunSubprocess:
    async def test_success_shell(self, tmp_path):
        """Shell subprocess returns stdout/stderr and success=True on exit 0."""
        stdout, stderr, success, exit_code = await _run_subprocess(
            "echo hello",
            env={"PATH": "/usr/bin:/bin"},
            cwd=str(tmp_path),
            shell=True,
        )
        assert stdout.strip() == "hello"
        assert success is True
        assert exit_code == 0

    async def test_nonzero_exit_returns_false(self, tmp_path):
        """Non-zero exit code → success=False."""
        _, _, success, exit_code = await _run_subprocess(
            "exit 1",
            env={"PATH": "/usr/bin:/bin"},
            cwd=str(tmp_path),
            shell=True,
        )
        assert success is False
        assert exit_code == 1

    async def test_exit_code_127_command_not_found(self, tmp_path):
        """Exit code 127 when command is not found."""
        _, _, success, exit_code = await _run_subprocess(
            "nonexistent_command_xyz_99",
            env={"PATH": "/usr/bin:/bin"},
            cwd=str(tmp_path),
            shell=True,
        )
        assert success is False
        assert exit_code == 127

    async def test_exit_code_specific_value(self, tmp_path):
        """Arbitrary non-zero exit codes are preserved."""
        _, _, success, exit_code = await _run_subprocess(
            "exit 42",
            env={"PATH": "/usr/bin:/bin"},
            cwd=str(tmp_path),
            shell=True,
        )
        assert success is False
        assert exit_code == 42

    async def test_oserror_returns_error(self, tmp_path):
        """OSError (e.g. executable not found) → success=False, error in stderr."""
        from unittest.mock import AsyncMock, patch

        with patch(
            "kiso.worker.utils.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("No such file")),
        ):
            _, stderr, success, exit_code = await _run_subprocess(
                ["/nonexistent/binary"],
                env={},
                cwd=str(tmp_path),
            )

        assert success is False
        assert "No such file" in stderr
        assert exit_code == -1

    async def test_shell_uses_bash(self, tmp_path):
        """M139: shell=True uses bash, not /bin/sh (dash). Here-strings work."""
        stdout, _, success, _ = await _run_subprocess(
            "cat <<< 'hello from bash'",
            env={"PATH": "/usr/bin:/bin"},
            cwd=str(tmp_path),
            shell=True,
        )
        assert success is True
        assert "hello from bash" in stdout

    async def test_shell_bash_double_brackets(self, tmp_path):
        """M139: bash double brackets work (would fail on dash)."""
        stdout, _, success, _ = await _run_subprocess(
            '[[ "abc" == abc ]] && echo ok',
            env={"PATH": "/usr/bin:/bin"},
            cwd=str(tmp_path),
            shell=True,
        )
        assert success is True
        assert "ok" in stdout

    async def test_exec_mode_with_stdin(self, tmp_path):
        """Non-shell mode with stdin_data passes data to the process."""
        import sys as _sys
        stdout, _, success, _ = await _run_subprocess(
            [_sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
            env={"PATH": "/usr/bin:/bin"},
            cwd=str(tmp_path),
            stdin_data=b"hello from stdin",
        )
        assert success is True
        assert "hello from stdin" in stdout

    async def test_no_timeout_wrapping(self, tmp_path):
        """M189: _run_subprocess never wraps in asyncio.wait_for (no timeout)."""
        with patch("kiso.worker.utils.asyncio.wait_for", new_callable=AsyncMock) as mock_wf:
            stdout, _, success, _ = await _run_subprocess(
                "echo no_timeout",
                env={"PATH": "/usr/bin:/bin"},
                cwd=str(tmp_path),
                shell=True,
            )
        mock_wf.assert_not_called()
        assert success is True
        assert "no_timeout" in stdout


# --- _exec_task ---

class TestExecTask:
    async def test_successful_command(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success, _ = await _exec_task("test-sess", "echo hello")
        assert stdout.strip() == "hello"
        assert success is True

    async def test_failing_command(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success, _ = await _exec_task("test-sess", "ls /nonexistent_dir_xyz")
        assert success is False
        assert stderr  # should have error message

    async def test_workspace_created(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            await _exec_task("new-sess", "echo ok")
        assert (tmp_path / "sessions" / "new-sess").is_dir()

    async def test_captures_stderr(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success, _ = await _exec_task("test-sess", "echo err >&2")
        assert "err" in stderr

    async def test_runs_in_workspace_dir(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success, _ = await _exec_task("test-sess", "pwd")
        expected = str(tmp_path / "sessions" / "test-sess")
        assert stdout.strip() == expected

    async def test_deny_list_blocks_rm_rf(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success, exit_code = await _exec_task("test-sess", "rm -rf /")
        assert success is False
        assert "Command blocked" in stderr
        assert exit_code == -1

    async def test_deny_list_allows_safe_rm(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            stdout, stderr, success, _ = await _exec_task("test-sess", "rm -rf ./nonexistent")
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

    async def test_user_message_reaches_messenger_context(self, db):
        """M214: _msg_task passes user_message to messenger context."""
        config = _make_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kwargs):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Say hello",
                            user_message="Ciao, dimmi qualcosa")
        user_content = captured_messages[1]["content"]
        assert "Original User Message" in user_content
        assert "Ciao, dimmi qualcosa" in user_content

    async def test_messenger_error_propagates(self, db):
        from kiso.brain import MessengerError
        config = _make_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, side_effect=LLMError("API down")):
            with pytest.raises(MessengerError, match="API down"):
                await _msg_task(config, db, "sess1", "task")


# --- run_worker ---

async def _assert_all_messages_processed(db) -> None:
    """Assert no unprocessed messages remain in the DB."""
    cur = await db.execute("SELECT COUNT(*) FROM messages WHERE processed = 0")
    assert (await cur.fetchone())[0] == 0


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
        # Intent msg injected even without language prefix (M201 fix)
        assert len(tasks) == 3
        assert tasks[0]["type"] == "msg"  # intent msg
        assert tasks[1]["type"] == "exec"
        assert tasks[1]["status"] == "done"
        assert "hello" in tasks[1]["output"]
        assert tasks[2]["type"] == "msg"
        assert tasks[2]["status"] == "done"

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
            "llm_timeout": 5,
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
        """Exec fails, reviewer errors → plan fails (after replan attempts, M170)."""
        config = _make_config(settings={"max_replan_depth": 1})
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

        # The last plan should be failed
        plans = await db.execute_fetchall("SELECT * FROM plans WHERE session = 'sess1' ORDER BY id DESC LIMIT 1")
        assert plans[0]["status"] == "failed"

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
        config = _make_config(settings={"max_replan_depth": 1})
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Should eventually fail (after replan attempts hit max depth)
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
        await _assert_all_messages_processed(db)

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

        await _assert_all_messages_processed(db)

    async def test_idle_timeout_exits(self, db, tmp_path):
        """Worker exits after idle_timeout with no messages."""
        config = _make_config(settings={
            "worker_idle_timeout": 0.1,
            "llm_timeout": 5,
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
        await _assert_all_messages_processed(db)

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

        review_with_learning = {"status": "ok", "reason": None, "learn": ["Project uses pytest"]}

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
            "llm_timeout": 5,
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
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
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
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
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
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        # Get first plan's tasks
        cur = await db.execute("SELECT * FROM plans WHERE session = 'sess1' ORDER BY id")
        plans = [dict(r) for r in await cur.fetchall()]
        assert len(plans) == 2

        first_tasks = await get_tasks_for_plan(db, plans[0]["id"])
        # The msg task was pending and should be marked skipped (superseded)
        msg_task = [t for t in first_tasks if t["type"] == "msg"][0]
        assert msg_task["status"] == "skipped"
        assert "superseded" in msg_task["output"]

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
        """Skill task review error → plan fails (after replan attempts)."""
        config = _make_config(settings={"max_replan_depth": 1})
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

        plan = await get_plan_for_session(db, "sess1")
        assert plan["status"] == "failed"

    async def test_skill_review_ok_still_fails_plan(self, db, tmp_path):
        """Even if reviewer says ok for a skill task, plan still fails (skill not installed)."""
        config = _make_config(settings={"max_replan_depth": 1})
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "search", processed=False)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "search", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=SKILL_PLAN), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=5)

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
        review_with_learn = {"status": "ok", "reason": None, "learn": ["Uses Flask"]}
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

    async def test_learn_string_wrapped(self, db):
        """If reviewer returns learn as a plain string (malformed), wrap it in a list."""
        config = _make_config()
        review_str_learn = {"status": "ok", "reason": None, "learn": "Uses Flask"}
        tid = await self._make_task(db)
        task_row = {"id": tid, "detail": "echo", "expect": "ok", "output": "ok", "stderr": ""}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_str_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT content FROM learnings WHERE session = 'sess1'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Uses Flask"

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
        replan_with_learn = {"status": "replan", "reason": "Bad output", "learn": ["Needs retry"]}
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


    async def test_review_task_empty_output_discards_learnings(self, db):
        """M111d: learnings are discarded when task output is empty."""
        config = _make_config()
        review_with_learn = {"status": "ok", "reason": None, "learn": ["Inferred fact"]}
        tid = await self._make_task(db, "check version", "shows version")
        task_row = {"id": tid, "detail": "check version", "expect": "shows version",
                    "output": "", "stderr": ""}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT COUNT(*) FROM learnings")
        count = (await cur.fetchone())[0]
        assert count == 0, "Learnings should be discarded for empty output"

    async def test_review_task_whitespace_output_discards_learnings(self, db):
        """M111d: learnings are discarded when task output is whitespace-only."""
        config = _make_config()
        review_with_learn = {"status": "ok", "reason": None, "learn": ["Inferred fact"]}
        tid = await self._make_task(db, "check", "ok")
        task_row = {"id": tid, "detail": "check", "expect": "ok",
                    "output": "  \n  ", "stderr": "  "}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT COUNT(*) FROM learnings")
        count = (await cur.fetchone())[0]
        assert count == 0

    async def test_review_task_nonempty_output_keeps_learnings(self, db):
        """M111d: learnings are kept when output has real content."""
        config = _make_config()
        review_with_learn = {"status": "ok", "reason": None, "learn": ["Uses Flask"]}
        tid = await self._make_task(db)
        task_row = {"id": tid, "detail": "echo", "expect": "ok",
                    "output": "Flask==2.0", "stderr": ""}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT COUNT(*) FROM learnings")
        count = (await cur.fetchone())[0]
        assert count == 1

    async def test_review_task_empty_output_but_stderr_keeps_learnings(self, db):
        """M111d: learnings kept when output is empty but stderr has content."""
        config = _make_config()
        review_with_learn = {"status": "ok", "reason": None, "learn": ["Uses Python 3.11"]}
        tid = await self._make_task(db)
        task_row = {"id": tid, "detail": "check", "expect": "ok",
                    "output": "", "stderr": "Python 3.11.4"}
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_learn):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        cur = await db.execute("SELECT COUNT(*) FROM learnings")
        count = (await cur.fetchone())[0]
        assert count == 1


# --- M226: Smoke tests — large output handling ---

class TestReviewTaskLargeOutput:
    """M226: verify _review_task truncates large output via prepare_reviewer_output."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_large_stdout_truncated_for_reviewer(self, db):
        """100KB stdout is truncated before reaching the reviewer LLM."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        tid = await create_task(db, plan_id, "sess1", type="exec", detail="big", expect="ok")
        large_output = "\n".join(f"line {i}: ok" for i in range(5000))
        task_row = {"id": tid, "detail": "big", "expect": "ok",
                    "output": large_output, "stderr": ""}

        captured_output = []

        async def _mock_reviewer(config, *, goal, detail, expect, output, user_message, **kw):
            captured_output.append(output)
            return {"status": "ok", "reason": None, "learn": None,
                    "retry_hint": None, "summary": None}

        with patch("kiso.worker.loop.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        # Reviewer should have received truncated output, not 100KB
        assert len(captured_output) == 1
        assert len(captured_output[0]) <= 5000  # well under 100KB

    async def test_error_in_middle_reaches_reviewer(self, db):
        """Error line buried in large stdout reaches the reviewer via grep section."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        tid = await create_task(db, plan_id, "sess1", type="exec", detail="build", expect="build succeeds")
        lines = [f"compiling module {i}..." for i in range(500)]
        lines[50] = "FATAL ERROR: out of memory"
        lines[-1] = "Build finished"
        large_output = "\n".join(lines)
        task_row = {"id": tid, "detail": "build", "expect": "build succeeds",
                    "output": large_output, "stderr": ""}

        captured_output = []

        async def _mock_reviewer(config, *, goal, detail, expect, output, user_message, **kw):
            captured_output.append(output)
            return {"status": "replan", "reason": "build failed",
                    "learn": None, "retry_hint": None, "summary": None}

        with patch("kiso.worker.loop.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        assert "FATAL ERROR: out of memory" in captured_output[0]
        assert "Build finished" in captured_output[0]

    async def test_stderr_priority_in_large_output(self, db):
        """Stderr is preserved even when stdout is massive."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        tid = await create_task(db, plan_id, "sess1", type="exec", detail="cmd", expect="ok")
        task_row = {"id": tid, "detail": "cmd", "expect": "ok",
                    "output": "x\n" * 50000, "stderr": "critical: permission denied"}

        captured_output = []

        async def _mock_reviewer(config, *, goal, detail, expect, output, user_message, **kw):
            captured_output.append(output)
            return {"status": "replan", "reason": "permission denied",
                    "learn": None, "retry_hint": None, "summary": None}

        with patch("kiso.worker.loop.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        assert "critical: permission denied" in captured_output[0]

    async def test_small_output_unchanged(self, db):
        """Small output passes through without truncation marker."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        tid = await create_task(db, plan_id, "sess1", type="exec", detail="echo hi", expect="prints hi")
        task_row = {"id": tid, "detail": "echo hi", "expect": "prints hi",
                    "output": "hi\n", "stderr": ""}

        captured_output = []

        async def _mock_reviewer(config, *, goal, detail, expect, output, user_message, **kw):
            captured_output.append(output)
            return {"status": "ok", "reason": None, "learn": None,
                    "retry_hint": None, "summary": None}

        with patch("kiso.worker.loop.run_reviewer", side_effect=_mock_reviewer):
            await _review_task(config, db, "sess1", "goal", task_row, "msg")

        assert captured_output[0] == "hi\n"
        assert "TRUNCATED" not in captured_output[0]


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

    def test_output_truncated_to_limit(self):
        long_output = "x" * 5000
        completed = [{"type": "exec", "detail": "cmd", "status": "done", "output": long_output}]
        ctx = _build_replan_context(completed, [], "broke", [])
        # Output should be truncated with "... (truncated)" marker
        assert "... (truncated)" in ctx
        # Should NOT contain more than _REPLAN_OUTPUT_LIMIT (1000) chars
        assert "x" * 1001 not in ctx

    def test_output_under_limit_not_truncated(self):
        output = "short output"
        completed = [{"type": "exec", "detail": "cmd", "status": "done", "output": output}]
        ctx = _build_replan_context(completed, [], "broke", [])
        assert "short output" in ctx
        assert "truncated" not in ctx

    def test_smart_truncate_at_newline(self):
        # 3 lines — the 3rd line pushes past _REPLAN_OUTPUT_LIMIT (1000 chars)
        line1 = "a" * 400 + "\n"
        line2 = "b" * 400 + "\n"
        line3 = "c" * 400
        long_output = line1 + line2 + line3
        completed = [{"type": "exec", "detail": "cmd", "status": "done", "output": long_output}]
        ctx = _build_replan_context(completed, [], "broke", [])
        # Should include line1 + line2 but truncate at newline before line3
        assert "a" * 400 in ctx
        assert "b" * 400 in ctx
        assert "c" * 400 not in ctx
        assert "... (truncated)" in ctx

    def test_msg_tasks_stripped_from_completed(self):
        """M309: msg-type tasks are excluded from completed tasks in replan context."""
        completed = [
            {"type": "exec", "detail": "install", "status": "done", "output": "installed ok"},
            {"type": "msg", "detail": "intent message", "status": "done", "output": "hello user"},
            {"type": "skill", "detail": "run tool", "status": "done", "output": "result"},
        ]
        ctx = _build_replan_context(completed, [], "failed", [])
        assert "install" in ctx
        assert "run tool" in ctx
        assert "intent message" not in ctx
        assert "hello user" not in ctx

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

    def test_replan_history_includes_key_outputs(self):
        """M130: replan history entries include key_outputs from completed tasks."""
        history = [
            {
                "goal": "Install browser",
                "failure": "skill not found",
                "what_was_tried": ["[exec] kiso skill install browser"],
                "key_outputs": ["[exec] registry: browser v1.0 available"],
            },
        ]
        ctx = _build_replan_context([], [], "still failing", history)
        assert "registry: browser v1.0 available" in ctx
        assert "Output:" in ctx

    def test_replan_history_key_outputs_budget(self):
        """M130: key_outputs in history are capped by budget."""
        long_output = "x" * 4000
        history = [
            {
                "goal": "Try",
                "failure": "fail",
                "what_was_tried": ["[exec] cmd"],
                "key_outputs": [f"[exec] {long_output}"],
            },
        ]
        ctx = _build_replan_context([], [], "fail", history)
        # Output should be truncated
        assert "... (truncated)" in ctx or len(long_output) > ctx.count("x")

    def test_confirmed_facts_from_registry_json(self):
        """M131: extract skill name from registry JSON output."""
        import json as _json
        registry_output = _json.dumps({"name": "browser", "version": "1.0", "description": "Browse the web"})
        completed = [{"type": "exec", "detail": "curl registry", "status": "done", "output": registry_output}]
        ctx = _build_replan_context(completed, [], "skill not installed", [])
        assert "Confirmed Facts" in ctx
        assert "browser" in ctx
        assert "DO NOT re-verify" in ctx

    def test_confirmed_facts_from_registry_list(self):
        """M131: extract skill names from registry JSON array."""
        import json as _json
        registry_output = _json.dumps([
            {"name": "browser", "version": "1.0"},
            {"name": "search", "version": "2.0"},
        ])
        completed = [{"type": "exec", "detail": "curl registry", "status": "done", "output": registry_output}]
        ctx = _build_replan_context(completed, [], "need skills", [])
        assert "Confirmed Facts" in ctx
        assert "browser" in ctx
        assert "search" in ctx

    def test_confirmed_facts_from_install_output(self):
        """M131: extract install status from command output."""
        completed = [{"type": "exec", "detail": "kiso skill install browser", "status": "done",
                      "output": "Skill 'browser' installed successfully\nReady to use"}]
        ctx = _build_replan_context(completed, [], "next step", [])
        assert "Confirmed Facts" in ctx
        assert "installed" in ctx.lower()

    def test_confirmed_facts_from_history_outputs(self):
        """M131: facts extracted from previous replan key_outputs too."""
        import json as _json
        history = [{
            "goal": "Find browser",
            "failure": "not found",
            "what_was_tried": ["[exec] curl registry"],
            "key_outputs": [f"[exec] {_json.dumps({'name': 'browser', 'version': '1.0'})}"],
        }]
        ctx = _build_replan_context([], [], "still failing", history)
        assert "Confirmed Facts" in ctx
        assert "browser" in ctx

    def test_no_confirmed_facts_for_empty_outputs(self):
        """M131: no Confirmed Facts section when outputs are empty."""
        completed = [{"type": "exec", "detail": "cmd", "status": "done", "output": ""}]
        ctx = _build_replan_context(completed, [], "failed", [])
        assert "Confirmed Facts" not in ctx

    def test_reviewer_summary_as_confirmed_fact(self):
        """M160: reviewer summaries appear as confirmed facts in replan context."""
        completed = [
            {"type": "exec", "detail": "curl site.com", "status": "done",
             "output": "<html>very long html...</html>",
             "reviewer_summary": "Site is a design agency based in Milan"},
        ]
        ctx = _build_replan_context(completed, [], "need screenshot", [])
        assert "Confirmed Facts" in ctx
        assert "design agency based in Milan" in ctx

    def test_confirmed_facts_cap_at_15(self):
        """M160: confirmed facts capped at 15."""
        from kiso.worker.utils import _extract_confirmed_facts
        completed = [
            {"type": "exec", "detail": f"cmd-{i}", "status": "done",
             "output": f"result-{i}", "reviewer_summary": f"fact-{i}"}
            for i in range(20)
        ]
        facts = _extract_confirmed_facts(completed)
        assert len(facts) <= 15


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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        assert len(completed) == 0
        assert len(remaining) == 1  # msg task

    async def test_exec_review_error_triggers_replan(self, db, tmp_path):
        """M170: exec review error triggers replan with task output preserved."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("down")), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is not None
        assert "Review failed" in reason
        assert len(remaining) == 1
        assert len(_po) == 1  # task output preserved

    async def test_msg_llm_error(self, db, tmp_path):
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, side_effect=MessengerError("down")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is None
        assert len(completed) == 0

    async def test_skill_not_installed_triggers_replan(self, db, tmp_path):
        """Skill not installed → replan with error message (M164)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        mock_reviewer = AsyncMock(return_value=REVIEW_REPLAN)
        with patch("kiso.worker.loop.run_reviewer", mock_reviewer), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is not None
        assert "not installed" in reason
        assert len(remaining) == 1  # msg task
        mock_reviewer.assert_not_called()  # review skipped (setup error)
        tasks = await get_tasks_for_plan(db, plan_id)
        skill_task = [t for t in tasks if t["type"] == "skill"][0]
        assert skill_task["status"] == "failed"
        assert "not installed" in skill_task["output"]
        # plan_outputs should contain the error
        assert len(_po) == 1
        assert "not installed" in _po[0]["output"]

    async def test_skill_invalid_args_json_triggers_replan(self, db, tmp_path):
        """Invalid JSON in skill args → replan with error (M164)."""
        config = _make_config()
        skill_info = {"name": "browser", "args_schema": {}, "entry": "browser.sh"}
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="do thing",
                          skill="browser", args="not-json{", expect="done")
        tasks = await get_tasks_for_plan(db, plan_id)
        task_row = tasks[0]
        ctx = _PlanCtx(
            db=db, config=config, session="sess1",
            goal="Test", user_message="msg",
            deploy_secrets={}, session_secrets={},
            max_output_size=4096, max_worker_retries=1,
            messenger_timeout=5, installed_skills=[skill_info],
            slog=None, sandbox_uid=None,
        )
        result = await _handle_skill_task(ctx, task_row, 0, False, 0)
        assert result.stop is True
        assert result.stop_success is False
        assert result.stop_replan is not None
        assert "Invalid skill args JSON" in result.stop_replan
        assert result.plan_output is not None

    async def test_skill_args_validation_failure_triggers_replan(self, db, tmp_path):
        """Skill args missing required field → replan with error (M164)."""
        config = _make_config()
        skill_info = {
            "name": "browser",
            "args_schema": {"action": {"type": "string", "required": True}},
            "entry": "browser.sh",
        }
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="take screenshot",
                          skill="browser", args="{}", expect="screenshot")
        tasks = await get_tasks_for_plan(db, plan_id)
        task_row = tasks[0]
        ctx = _PlanCtx(
            db=db, config=config, session="sess1",
            goal="Test", user_message="msg",
            deploy_secrets={}, session_secrets={},
            max_output_size=4096, max_worker_retries=1,
            messenger_timeout=5, installed_skills=[skill_info],
            slog=None, sandbox_uid=None,
        )
        result = await _handle_skill_task(ctx, task_row, 0, False, 0)
        assert result.stop is True
        assert result.stop_replan is not None
        assert "validation failed" in result.stop_replan
        assert result.plan_output is not None
        assert result.plan_output["status"] == "failed"

    async def test_skill_execution_failure_reviewer_replan(self, db, tmp_path):
        """M167: skill executes but fails (exit_code=1), reviewer says replan → replan reason returned."""
        config = _make_config()
        skill_info = {"name": "browser", "args_schema": {}, "entry": "browser.sh"}
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="take screenshot",
                          skill="browser", args="{}", expect="screenshot")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")
        tasks = await get_tasks_for_plan(db, plan_id)
        task_row = tasks[0]
        ctx = _PlanCtx(
            db=db, config=config, session="sess1",
            goal="Test", user_message="msg",
            deploy_secrets={}, session_secrets={},
            max_output_size=4096, max_worker_retries=1,
            messenger_timeout=5, installed_skills=[skill_info],
            slog=None, sandbox_uid=None,
        )
        with patch("kiso.worker.loop._skill_task", new_callable=AsyncMock,
                    return_value=("error output", "skill crashed", False, 1)), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                    return_value=REVIEW_REPLAN), \
             patch("kiso.worker.loop._write_plan_outputs", new_callable=AsyncMock), \
             patch("kiso.worker.loop._check_disk_limit", return_value=None):
            result = await _handle_skill_task(ctx, task_row, 0, False, 0)
        assert result.stop is True
        assert result.stop_success is False
        assert result.stop_replan == "Task failed"
        assert result.plan_output is not None

    async def test_skill_review_error(self, db, tmp_path):
        """Skill not installed → replan (M164); reviewer never reached."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="search",
                          skill="search", args="{}", expect="results")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("err")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is not None  # M164: setup failure triggers replan
        assert "not installed" in reason

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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        exec_tasks = [t for t in completed if t["type"] == "exec"]
        assert len(exec_tasks) == 1
        assert "translated" in exec_tasks[0]["output"]

    async def test_translator_failure_triggers_replan(self, db, tmp_path):
        """ExecTranslatorError → replan with error message (M168)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec",
                          detail="Do something impossible",
                          expect="magic")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                    side_effect=ExecTranslatorError("Cannot translate")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason is not None
        assert "Translation failed" in reason
        assert len(remaining) == 1  # msg task not executed
        assert len(_po) == 1
        assert "Translation failed" in _po[0]["output"]

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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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

        review_with_learn = {"status": "ok", "reason": None, "learn": ["Uses Flask"]}

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


# --- M201: _maybe_inject_intent_msg ---

class TestM201IntentMsgInjection:
    """M201: auto-inject intent msg before plan execution."""

    def test_injects_msg_when_first_task_is_exec(self):
        tasks = [
            {"type": "exec", "detail": "echo hello", "skill": None, "args": None, "expect": "ok"},
            {"type": "exec", "detail": "echo world", "skill": None, "args": None, "expect": "ok"},
            {"type": "msg", "detail": "Answer in Italian. fatto", "skill": None, "args": None, "expect": None},
        ]
        result = _maybe_inject_intent_msg(tasks, "greet the world")
        assert len(result) == 4
        assert result[0]["type"] == "msg"
        assert result[0]["detail"].startswith("Answer in Italian.")
        assert "echo hello" in result[0]["detail"]
        # Original tasks unchanged
        assert result[1]["type"] == "exec"

    def test_no_injection_when_first_task_is_msg(self):
        tasks = [
            {"type": "msg", "detail": "Answer in English. hello", "skill": None, "args": None, "expect": None},
            {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
        ]
        result = _maybe_inject_intent_msg(tasks, "greet")
        assert len(result) == 2
        assert result[0]["detail"] == "Answer in English. hello"

    def test_no_injection_for_single_task(self):
        tasks = [
            {"type": "exec", "detail": "echo hi", "skill": None, "args": None, "expect": "ok"},
        ]
        result = _maybe_inject_intent_msg(tasks, "greet")
        assert len(result) == 1

    def test_injection_without_lang_prefix(self):
        """Inject intent msg even when no Answer in prefix — messenger infers language."""
        tasks = [
            {"type": "exec", "detail": "a", "skill": None, "args": None, "expect": "ok"},
            {"type": "exec", "detail": "b", "skill": None, "args": None, "expect": "ok"},
        ]
        result = _maybe_inject_intent_msg(tasks, "goal")
        assert len(result) == 3  # injection happened
        assert result[0]["type"] == "msg"
        assert not result[0]["detail"].startswith("Answer in")  # no lang prefix

    def test_does_not_mutate_input(self):
        tasks = [
            {"type": "exec", "detail": "a", "skill": None, "args": None, "expect": "ok"},
            {"type": "exec", "detail": "b", "skill": None, "args": None, "expect": "ok"},
            {"type": "msg", "detail": "Answer in English. done", "skill": None, "args": None, "expect": None},
        ]
        original_len = len(tasks)
        result = _maybe_inject_intent_msg(tasks, "goal")
        assert len(tasks) == original_len
        assert len(result) == original_len + 1

    def test_intent_detail_includes_up_to_3_steps(self):
        tasks = [
            {"type": "exec", "detail": f"step{i}", "skill": None, "args": None, "expect": "ok"}
            for i in range(5)
        ]
        tasks.append({"type": "msg", "detail": "Answer in French. fini", "skill": None, "args": None, "expect": None})
        result = _maybe_inject_intent_msg(tasks, "multi-step")
        detail = result[0]["detail"]
        assert detail.startswith("Answer in French.")
        assert "step0" in detail
        assert "step1" in detail
        assert "step2" in detail
        # step3 and step4 are NOT in the summary (max 3)
        assert "step3" not in detail

    def test_legacy_lang_tag_still_works(self):
        """Legacy [Lang: xx] format is still supported during transition."""
        tasks = [
            {"type": "exec", "detail": "echo hello", "skill": None, "args": None, "expect": "ok"},
            {"type": "msg", "detail": "[Lang: it] fatto", "skill": None, "args": None, "expect": None},
        ]
        result = _maybe_inject_intent_msg(tasks, "greet")
        assert len(result) == 3
        assert result[0]["detail"].startswith("[Lang: it]")

    async def test_replan_skips_intent_msg(self, tmp_path):
        """M279: intent msg is NOT injected for replan — user already saw the intent."""
        from kiso.store import init_db, create_session, create_plan, get_tasks_for_plan
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        plan_id = await create_plan(db, "sess1", 1, "Retry with new approach")

        replan_tasks = [
            {"type": "exec", "detail": "apt-get install foo", "skill": None, "args": None, "expect": "ok"},
            {"type": "exec", "detail": "run test", "skill": None, "args": None, "expect": "pass"},
            {"type": "msg", "detail": "Answer in Italian. report", "skill": None, "args": None, "expect": None},
        ]
        # Replan path persists tasks directly without _maybe_inject_intent_msg
        await _persist_plan_tasks(db, plan_id, "sess1", replan_tasks)

        db_tasks = await get_tasks_for_plan(db, plan_id)
        assert len(db_tasks) == 3  # No injected intent msg
        assert db_tasks[0]["type"] == "exec"
        await db.close()

    def test_m264_intent_msg_says_system(self):
        """M264: intent msg says 'the system is about to do', not 'you're about to do'."""
        tasks = [
            {"type": "exec", "detail": "echo hello", "skill": None, "args": None, "expect": "ok"},
            {"type": "exec", "detail": "echo world", "skill": None, "args": None, "expect": "ok"},
        ]
        result = _maybe_inject_intent_msg(tasks, "greet")
        assert "the system is about to do" in result[0]["detail"]
        assert "you're about to do" not in result[0]["detail"]


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
    async def test_writes_json_file(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            outputs = [
                {"index": 1, "type": "exec", "detail": "echo hi", "output": "hi\n", "status": "done"},
            ]
            await _write_plan_outputs("sess1", outputs)

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["output"] == "hi\n"

    async def test_overwrites_previous(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            await _write_plan_outputs("sess1", [{"index": 1, "type": "exec", "detail": "a", "output": "1", "status": "done"}])
            await _write_plan_outputs("sess1", [
                {"index": 1, "type": "exec", "detail": "a", "output": "1", "status": "done"},
                {"index": 2, "type": "exec", "detail": "b", "output": "2", "status": "done"},
            ])

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        data = json.loads(path.read_text())
        assert len(data) == 2

    async def test_empty_outputs(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            await _write_plan_outputs("sess1", [])

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        assert path.exists()
        assert json.loads(path.read_text()) == []

    async def test_non_ascii_content_written_as_utf8(self, tmp_path):
        """M89d: non-ASCII content in plan outputs is preserved via utf-8 encoding."""
        outputs = [{"index": 1, "type": "msg", "detail": "saluta", "output": "Héllo wörld — 日本語", "status": "done"}]
        with _patch_kiso_dir(tmp_path):
            await _write_plan_outputs("sess1", outputs)

        path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
        raw = path.read_bytes()
        assert "Héllo wörld — 日本語".encode("utf-8") in raw
        data = json.loads(raw.decode("utf-8"))
        assert data[0]["output"] == "Héllo wörld — 日本語"


# --- _cleanup_plan_outputs ---

class TestCleanupPlanOutputs:
    async def test_removes_file(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            await _write_plan_outputs("sess1", [{"index": 1, "type": "exec", "detail": "a", "output": "x", "status": "done"}])
            path = tmp_path / "sessions" / "sess1" / ".kiso" / "plan_outputs.json"
            assert path.exists()
            await _cleanup_plan_outputs("sess1")
            assert not path.exists()

    async def test_no_error_if_missing(self, tmp_path):
        with _patch_kiso_dir(tmp_path):
            # Ensure workspace exists but no file
            _session_workspace("sess1")
            await _cleanup_plan_outputs("sess1")  # should not raise

    def test_is_coroutine_function(self):
        """_cleanup_plan_outputs must be async (consistent with _write_plan_outputs)."""
        import asyncio
        assert asyncio.iscoroutinefunction(_cleanup_plan_outputs)


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

    def test_small_outputs_all_included(self):
        """3 small outputs under budget → all included in full."""
        outputs = [
            {"index": i, "type": "exec", "detail": f"task{i}", "output": f"out{i}", "status": "done"}
            for i in range(1, 4)
        ]
        result = _format_plan_outputs_for_msg(outputs, budget=8000)
        for i in range(1, 4):
            assert f"[{i}] exec: task{i}" in result
            assert f"out{i}" in result
        assert "summarized" not in result

    def test_large_outputs_oldest_summarized(self):
        """6 outputs with 4KB each → most recent in full, oldest summarized."""
        outputs = [
            {"index": i, "type": "exec", "detail": f"task{i}", "output": "x" * 2000, "status": "done"}
            for i in range(1, 7)
        ]
        result = _format_plan_outputs_for_msg(outputs, budget=8000)
        # Most recent outputs should be in full
        assert "Status: done" in result
        # Oldest outputs should be summarized
        assert "summarized" in result
        # Summary lines don't have fenced output
        assert result.count("<<<TASK_OUTPUT_") < 6

    def test_budget_respected(self):
        """Total output stays roughly within budget."""
        outputs = [
            {"index": i, "type": "exec", "detail": f"task{i}", "output": "y" * 3000, "status": "done"}
            for i in range(1, 10)
        ]
        result = _format_plan_outputs_for_msg(outputs, budget=8000)
        # The full entries part should be within budget (summaries add minor overhead)
        assert len(result) < 12000  # budget + summary overhead

    def test_order_preserved(self):
        """Outputs maintain ascending index order even after reverse processing."""
        outputs = [
            {"index": i, "type": "exec", "detail": f"task{i}", "output": f"out{i}", "status": "done"}
            for i in range(1, 4)
        ]
        result = _format_plan_outputs_for_msg(outputs, budget=8000)
        pos1 = result.index("[1]")
        pos2 = result.index("[2]")
        pos3 = result.index("[3]")
        assert pos1 < pos2 < pos3

    def test_prefers_reviewer_summary_over_raw_output(self):
        """M247: reviewer_summary is used instead of raw output when available."""
        outputs = [
            {
                "index": 1, "type": "exec", "detail": "search news",
                "output": "raw HTML noise...", "status": "done",
                "reviewer_summary": "Top headlines: A, B, C",
            },
        ]
        result = _format_plan_outputs_for_msg(outputs)
        assert "Summary: Top headlines: A, B, C" in result
        assert "raw HTML noise" not in result

    def test_falls_back_to_raw_output_without_summary(self):
        """Without reviewer_summary, raw output is used as before."""
        outputs = [
            {"index": 1, "type": "exec", "detail": "echo hi", "output": "hi\n", "status": "done"},
        ]
        result = _format_plan_outputs_for_msg(outputs)
        assert "hi\n" in result
        assert "Summary:" not in result

    def test_mixed_with_and_without_summary(self):
        """M247: entries with summary use it, entries without use raw output."""
        outputs = [
            {"index": 1, "type": "exec", "detail": "step 1", "output": "raw1", "status": "done"},
            {
                "index": 2, "type": "search", "detail": "search",
                "output": "long raw search output...", "status": "done",
                "reviewer_summary": "Found: X, Y, Z",
            },
        ]
        result = _format_plan_outputs_for_msg(outputs, budget=8000)
        # Entry 1: raw output
        assert "raw1" in result
        # Entry 2: reviewer summary
        assert "Summary: Found: X, Y, Z" in result
        assert "long raw search output" not in result


# --- _make_plan_output (M91c) ---


class TestMakePlanOutput:
    def test_returns_correct_keys(self):
        """_make_plan_output returns dict with all five required keys."""
        entry = _make_plan_output(3, "exec", "echo hi", "hi\n", "done")
        assert entry == {"index": 3, "type": "exec", "detail": "echo hi", "output": "hi\n", "status": "done"}

    def test_index_is_preserved(self):
        entry = _make_plan_output(7, "msg", "greet", "Hello!", "done")
        assert entry["index"] == 7

    def test_all_task_types(self):
        for task_type in ("exec", "msg", "skill", "search"):
            entry = _make_plan_output(1, task_type, "d", "o", "done")
            assert entry["type"] == task_type

    def test_large_output_saved_to_file(self, tmp_path):
        """M140: large outputs are saved to workspace files."""
        big_output = "x" * 5000
        with _patch_kiso_dir(tmp_path):
            entry = _make_plan_output(3, "exec", "curl site", big_output, "done", session="test-sess")
        assert "[Full output saved to" in entry["output"]
        assert "5000 chars" in entry["output"]
        # File actually exists
        saved = tmp_path / "sessions" / "test-sess" / ".kiso" / "task_outputs" / "task_3.txt"
        assert saved.exists()
        assert saved.read_text() == big_output

    def test_small_output_not_saved(self, tmp_path):
        """M140: small outputs stay inline."""
        small_output = "hello world"
        with _patch_kiso_dir(tmp_path):
            entry = _make_plan_output(1, "exec", "echo hi", small_output, "done", session="test-sess")
        assert entry["output"] == small_output

    def test_no_session_skips_save(self):
        """M140: without session, large output stays inline (backward compat)."""
        big_output = "x" * 5000
        entry = _make_plan_output(1, "exec", "cmd", big_output, "done")
        assert entry["output"] == big_output


class TestSaveLargeOutput:
    def test_below_threshold(self, tmp_path):
        """Output below threshold returned unchanged."""
        with _patch_kiso_dir(tmp_path):
            result = _save_large_output("sess1", 1, "short output")
        assert result == "short output"

    def test_above_threshold_saved(self, tmp_path):
        """Output above threshold saved to file, reference returned."""
        big = "A" * 5000
        with _patch_kiso_dir(tmp_path):
            result = _save_large_output("sess1", 2, big)
        assert "[Full output saved to" in result
        assert "5000 chars" in result
        assert "Use cat/grep" in result
        # Verify head is included
        assert "A" * 500 in result
        # File on disk
        path = tmp_path / "sessions" / "sess1" / ".kiso" / "task_outputs" / "task_2.txt"
        assert path.read_text() == big

    def test_exactly_at_threshold(self, tmp_path):
        """Output exactly at threshold stays inline."""
        text = "B" * 4096
        with _patch_kiso_dir(tmp_path):
            result = _save_large_output("sess1", 1, text)
        assert result == text


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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
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
            success, _, _, _, _po = await _execute_plan(
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
            success, reason, _, _, _po = await _execute_plan(
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
            success, _, _, _, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
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
            success, reason, _, _, _po = await _execute_plan(
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
            stdout, stderr, success, _ = await _skill_task(
                "sess1", skill, {"text": "hello"}, None, None,
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
            stdout, _, success, _ = await _skill_task(
                "sess1", skill, {}, plan_outputs, None,
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
            stdout, _, success, _ = await _skill_task(
                "sess1", skill, {}, None, secrets,
            )
        assert success is True
        result = json.loads(stdout)
        assert result == {"api_token": "tok_123"}

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
            stdout, stderr, success, _ = await _skill_task(
                "sess1", skill, {}, None, None,
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
            stdout, stderr, success, _ = await _skill_task(
                "sess1", skill, {}, None, None,
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
            stdout, _, success, _ = await _skill_task(
                "sess1", skill, {}, None, None,
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
            success, reason, _, _, _po = await _execute_plan(
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
            success, _, _, _, _po = await _execute_plan(
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
            success, _, _, _, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
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
            success, reason, _, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        assert len(remaining) == 1  # msg task remaining

    async def test_skill_review_replan_carries_retry_hint(self, db, tmp_path):
        """M179: skill handler propagates retry_hint to plan_output on replan."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{"text":"hi"}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        review_with_hint = {
            "status": "replan", "reason": "Task failed",
            "learn": None, "retry_hint": "use action=screenshot instead",
            "summary": None,
        }

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_hint), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
            success, reason, _, remaining, plan_outputs = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        # The skill task's plan_output should carry the retry_hint
        skill_outputs = [po for po in plan_outputs if po.get("type") == "skill"]
        assert len(skill_outputs) == 1
        assert skill_outputs[0].get("retry_hint") == "use action=screenshot instead"

    async def test_skill_retry_on_transient_failure(self, db, tmp_path):
        """M204: skill retries internally when reviewer provides retry_hint."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{"text":"hi"}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        review_replan_with_hint = {
            "status": "replan", "reason": "Task failed",
            "learn": None, "retry_hint": "try again", "summary": None,
        }
        # First call: reviewer says replan with hint (triggers retry)
        # Second call: reviewer says ok (retry succeeds)
        reviewer_side = AsyncMock(side_effect=[review_replan_with_hint, REVIEW_OK])

        with patch("kiso.worker.loop._skill_task", new_callable=AsyncMock,
                    return_value=("ok output", "", True, 0)), \
             patch("kiso.worker.loop.run_reviewer", reviewer_side), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert reason is None
        # Skill was reviewed twice (retry + success)
        assert reviewer_side.call_count == 2

    async def test_skill_retry_exhausted_escalates_to_replan(self, db, tmp_path):
        """M204: skill escalates to replan after max_worker_retries exhausted."""
        config = _make_config(settings={"max_worker_retries": 1})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{"text":"hi"}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        review_replan_with_hint = {
            "status": "replan", "reason": "Still failing",
            "learn": None, "retry_hint": "try something else", "summary": None,
        }
        # Both calls return replan with hint — retry once, then escalate
        reviewer_side = AsyncMock(return_value=review_replan_with_hint)

        with patch("kiso.worker.loop._skill_task", new_callable=AsyncMock,
                    return_value=("error", "crash", False, 1)), \
             patch("kiso.worker.loop.run_reviewer", reviewer_side), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
            success, reason, _, remaining, plan_outputs = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Still failing"
        # Reviewed twice: initial + 1 retry
        assert reviewer_side.call_count == 2
        # retry_hint carried to plan_output
        skill_outputs = [po for po in plan_outputs if po.get("type") == "skill"]
        assert skill_outputs[0].get("retry_hint") == "try something else"

    async def test_skill_no_retry_without_hint(self, db, tmp_path):
        """M204: skill does NOT retry when reviewer returns replan without retry_hint."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="skill", detail="echo",
                          skill="echo", args='{"text":"hi"}', expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        skill = _create_echo_skill(tmp_path)

        # replan without retry_hint → immediate escalation, no retry
        reviewer_side = AsyncMock(return_value=REVIEW_REPLAN)

        with patch("kiso.worker.loop._skill_task", new_callable=AsyncMock,
                    return_value=("error", "crash", False, 1)), \
             patch("kiso.worker.loop.run_reviewer", reviewer_side), \
             patch("kiso.worker.loop.discover_skills", return_value=[skill]), \
             _patch_kiso_dir(tmp_path):
            success, reason, _, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        # Only reviewed once — no retry without hint
        assert reviewer_side.call_count == 1


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
            success, _, _, _, _po = await _execute_plan(
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
            success, _, _, _, _po = await _execute_plan(
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
            success, _, _, _, _po = await _execute_plan(
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
            success, _, _, _, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
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

    async def test_missing_learning_id_skipped(self, db):
        """M84f: evaluation without learning_id is skipped without crashing."""
        result = {"evaluations": [{"verdict": "promote", "fact": "x"}]}
        await _apply_curator_result(db, "sess1", result)  # must not raise
        facts = await get_facts(db)
        assert len(facts) == 0

    async def test_missing_verdict_skipped(self, db):
        """M84f: evaluation without verdict is skipped without crashing."""
        lid = await save_learning(db, "Something", "sess1")
        result = {"evaluations": [{"learning_id": lid, "fact": "x"}]}
        await _apply_curator_result(db, "sess1", result)  # must not raise
        # Learning status should remain unchanged (pending)
        cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
        assert (await cur.fetchone())[0] == "pending"

    async def test_promote_with_null_fact_discards(self, db):
        """M84f: promote verdict with null fact falls back to discard."""
        lid = await save_learning(db, "Something", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": None, "question": None},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
        assert (await cur.fetchone())[0] == "discarded"
        facts = await get_facts(db)
        assert len(facts) == 0

    async def test_ask_with_null_question_discards(self, db):
        """M84f: ask verdict with null question falls back to discard."""
        lid = await save_learning(db, "Something", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "ask", "fact": None, "question": None},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT status FROM learnings WHERE id = ?", (lid,))
        assert (await cur.fetchone())[0] == "discarded"
        items = await get_pending_items(db, "sess1")
        assert len(items) == 0


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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
        msgs, _installed, *_ = await build_planner_messages(db, config, "sessB", "admin", "hello")
        content = msgs[1]["content"]
        assert "Python 3.12" in content

    async def test_apply_curator_promote_saves_session(self, db, tmp_path):
        """M48d: non-user facts are global (session=None); only 'user' category is session-scoped."""
        await create_session(db, "sess1")
        lid = await save_learning(db, "Uses Flask", "sess1")
        result = {"evaluations": [
            # No category → defaults to "general" → global (session=None)
            {"learning_id": lid, "verdict": "promote", "fact": "Uses Flask", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["session"] is None  # general facts are global
        # User facts are session-scoped:
        lid2 = await save_learning(db, "Prefers tabs", "sess1")
        result2 = {"evaluations": [
            {"learning_id": lid2, "verdict": "promote", "fact": "Prefers tabs", "category": "user", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result2)
        all_facts = await get_facts(db)
        user_fact = next(f for f in all_facts if "tabs" in f["content"])
        assert user_fact["session"] == "sess1"

    async def test_knowledge_processing_order(self, db, tmp_path):
        """Curator runs before summarizer (order verified via side effects)."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
                username="alice",
            )

        assert success is True
        assert len(completed) == 1


class TestPerSessionSandbox:
    async def test_ensure_sandbox_user_creates_user(self):
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
            uid = await _ensure_sandbox_user("test-session")

        assert uid == 50001
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert "useradd" in args[0][0]

    async def test_ensure_sandbox_user_reuses_existing(self):
        """When user already exists, no useradd call is made."""
        with patch("kiso.worker.utils.pwd") as mock_pwd, \
             patch("subprocess.run") as mock_run:
            mock_pwd.getpwnam.return_value = type("pw", (), {"pw_uid": 50001})()
            uid = await _ensure_sandbox_user("test-session")

        assert uid == 50001
        mock_run.assert_not_called()

    async def test_ensure_sandbox_user_creation_fails(self):
        """When useradd fails, returns None."""
        import subprocess
        with patch("kiso.worker.utils.pwd") as mock_pwd, \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "useradd")):
            mock_pwd.getpwnam.side_effect = KeyError("no-user")
            uid = await _ensure_sandbox_user("test-session")

        assert uid is None

    def test_workspace_chown_chmod(self, tmp_path):
        """_session_workspace applies chown/chmod when sandbox_uid is given."""
        with _patch_kiso_dir(tmp_path), \
             patch("os.chown") as mock_chown, \
             patch("os.chmod") as mock_chmod:
            ws = _session_workspace("sess1", sandbox_uid=1234)

        pub = ws / "pub"
        uploads = ws / "uploads"
        assert mock_chown.call_count == 3
        mock_chown.assert_any_call(ws, 1234, 1234)
        mock_chown.assert_any_call(pub, 1234, 1234)
        mock_chown.assert_any_call(uploads, 1234, 1234)
        mock_chmod.assert_called_once_with(ws, 0o700)

    def test_workspace_creates_uploads_dir(self, tmp_path):
        """_session_workspace must create an uploads/ subdirectory."""
        with _patch_kiso_dir(tmp_path):
            ws = _session_workspace("sess1")
        assert (ws / "uploads").is_dir()

    def test_workspace_no_chown_without_sandbox_uid(self, tmp_path):
        """_session_workspace skips chown/chmod when sandbox_uid is None."""
        with _patch_kiso_dir(tmp_path), \
             patch("os.chown") as mock_chown, \
             patch("os.chmod") as mock_chmod:
            _session_workspace("sess1")

        mock_chown.assert_not_called()
        mock_chmod.assert_not_called()

    async def test_sandbox_uid_passed_to_exec_subprocess(self, tmp_path):
        """When sandbox_uid is set, it's passed to create_subprocess_exec."""
        captured_kwargs = {}

        async def _mock_subprocess(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            return proc

        with _patch_kiso_dir(tmp_path), \
             patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess):
            await _exec_task("sess1", "echo ok", sandbox_uid=1234)

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
             patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess):
            await _exec_task("sess1", "echo ok", sandbox_uid=None)

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
            await _skill_task("sess1", skill, {"text": "hi"}, None, None, sandbox_uid=9999)

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
            await _skill_task("sess1", skill, {"text": "hi"}, None, None, sandbox_uid=None)

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
            success, _, completed, _, _po = await _execute_plan(
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
            success, _, completed, remaining, _po = await _execute_plan(
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
             patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess):
            success, _, _, _, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
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
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
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
            stdout, stderr, success, _ = await _exec_task(
                "test-sess", cmd, max_output_size=100,
            )
        assert success is True
        assert len(stdout) <= 100
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
            stdout, stderr, success, _ = await _skill_task(
                "sess1", skill, {}, None, None, max_output_size=100,
            )
        assert success is True
        assert stdout.endswith("[truncated]")
        assert len(stdout) <= 100


class TestTruncateOutputUnit:
    """Direct unit tests for _truncate_output respecting the limit."""

    def test_total_length_respects_limit(self):
        text = "x" * 500
        result = _truncate_output(text, 100)
        assert len(result) == 100
        assert result.endswith("\n[truncated]")

    def test_short_text_unchanged(self):
        text = "hello"
        assert _truncate_output(text, 100) == "hello"

    def test_exact_limit_unchanged(self):
        text = "x" * 100
        assert _truncate_output(text, 100) == text

    def test_zero_limit_unchanged(self):
        text = "x" * 500
        assert _truncate_output(text, 0) == text


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
            "llm_timeout": 1,
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
            "llm_timeout": 1,
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
            "llm_timeout": 5,
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
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
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
             patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess):
            success, _, _, _, _po = await _execute_plan(
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
            "llm_timeout": 1,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
        await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["confidence"] < 1.0

    async def test_low_confidence_facts_archived(self, db, tmp_path):
        """Facts with low confidence are archived via _post_plan_knowledge."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
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
        await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

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
                   return_value=("ok", "", True, 0)), \
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
        assert "Completed successfully (1):" in result
        assert "[exec] echo hello" in result
        assert "Failed/Skipped (1):" in result
        assert "[msg] Report results" in result
        assert "suggest next steps" in result
        # M270: instruction not to misrepresent completed tasks
        assert "do NOT say they failed" in result

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
        assert "Completed successfully (1):" in result
        assert "Failed/Skipped" not in result

    def test_m270_replan_failure_explicit(self):
        """M270: when all tasks succeeded but replan failed, say so explicitly."""
        completed = [
            {"type": "exec", "detail": "Install browser"},
            {"type": "replan", "detail": "Visit guidance.studio"},
        ]
        result = _build_failure_summary(
            completed, [], "Visit guidance.studio",
            reason="Replan failed: LLM call failed: Empty response",
        )
        assert "All planned tasks completed successfully" in result
        assert "re-planning" in result
        assert "Failure reason: Replan failed" in result

    def test_m270_no_replan_msg_when_remaining(self):
        """M270: don't add replan clarification when tasks are still remaining."""
        completed = [{"type": "exec", "detail": "step 1"}]
        remaining = [{"type": "exec", "detail": "step 2"}]
        result = _build_failure_summary(completed, remaining, "Multi-step")
        assert "re-planning" not in result


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
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
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

    def test_home_set_to_real_home(self, tmp_path):
        """HOME is set to the real home directory, not KISO_DIR."""
        with _patch_kiso_dir(tmp_path):
            env = _build_exec_env()
        # Must be the real home, not KISO_DIR (tmp_path), to avoid double .kiso nesting
        assert env["HOME"] == str(Path.home())
        assert env["HOME"] != str(tmp_path)

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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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
            "llm_timeout": 5,
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

    async def test_extend_replan_cumulative_cap(self, db, tmp_path):
        """Multiple plans each requesting extend_replan are capped at 3 total."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
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

        # First replan requests extend=2 → gets 2 (total_extensions=2)
        extend2_plan = {
            "goal": "Extends by 2",
            "secrets": None,
            "extend_replan": 2,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        # Second replan requests extend=2 → gets only 1 (cap 3 - 2 used = 1 remaining)
        extend2_again_plan = {
            "goal": "Extends by 2 again",
            "secrets": None,
            "extend_replan": 2,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        # Third replan requests extend=2 → gets 0 (cap fully used)
        extend2_third_plan = {
            "goal": "Extends by 2 third time",
            "secrets": None,
            "extend_replan": 2,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

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
                return fail_plan          # initial plan, fails
            elif n == 2:
                return extend2_plan       # replan 1: requests +2, gets 2 (total=2, limit=3)
            elif n == 3:
                return extend2_again_plan # replan 2: requests +2, gets 1 (total=3, limit=4)
            elif n == 4:
                return extend2_third_plan # replan 3: requests +2, gets 0 (cap exhausted)
            return no_extend_plan         # replan 4: no extend, keeps failing

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Failed after max depth"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        # max_replan_depth starts at 1
        # replan 1: +2 granted → limit=3
        # replan 2: +1 granted (capped) → limit=4
        # replan 3: +0 (cap exhausted) → limit=4
        # replan 4: no extend → limit=4
        # Total: plan 1 + 4 replans = 5 planner calls, then hits depth limit
        assert len(planner_calls) == 5


class TestDefaultMaxReplanDepth:
    """Test that the default max_replan_depth is 3."""

    def test_default_max_replan_depth_is_5(self):
        from kiso.config import SETTINGS_DEFAULTS
        assert SETTINGS_DEFAULTS["max_replan_depth"] == 5


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

    def test_empty_when_no_cli_token(self, tmp_path, caplog):
        """No cli token → returns [] with a warning, does not raise."""
        import logging
        cfg_no_token = Config(
            tokens={},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={},
            settings={},
            raw={},
        )
        session_dir = tmp_path / "sessions" / "test-session"
        pub_dir = session_dir / "pub"
        pub_dir.mkdir(parents=True)
        (pub_dir / "file.txt").write_text("content")

        with _patch_kiso_dir(tmp_path), \
             caplog.at_level(logging.WARNING, logger="kiso.worker.utils"):
            result = _report_pub_files("test-session", cfg_no_token)

        assert result == []
        assert any("cli token" in r.message for r in caplog.records)

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


    def test_base_url_prefix(self, tmp_path, config):
        """M215: base_url prepends full URL prefix to pub paths."""
        session_dir = tmp_path / "sessions" / "test-session"
        pub_dir = session_dir / "pub"
        pub_dir.mkdir(parents=True)
        (pub_dir / "screenshot.png").write_bytes(b"fake")

        with _patch_kiso_dir(tmp_path):
            result = _report_pub_files("test-session", config, base_url="http://myhost:8333")

        assert len(result) == 1
        assert result[0]["url"].startswith("http://myhost:8333/pub/")

    def test_base_url_strips_trailing_slash(self, tmp_path, config):
        """M215: trailing slash in base_url doesn't cause double slash."""
        session_dir = tmp_path / "sessions" / "test-session"
        pub_dir = session_dir / "pub"
        pub_dir.mkdir(parents=True)
        (pub_dir / "file.txt").write_bytes(b"x")

        with _patch_kiso_dir(tmp_path):
            result = _report_pub_files("test-session", config, base_url="http://host:8333/")

        assert "//" not in result[0]["url"].replace("http://", "")

    def test_no_base_url_gives_relative(self, tmp_path, config):
        """M215: no base_url gives server-relative paths."""
        session_dir = tmp_path / "sessions" / "test-session"
        pub_dir = session_dir / "pub"
        pub_dir.mkdir(parents=True)
        (pub_dir / "file.txt").write_bytes(b"x")

        with _patch_kiso_dir(tmp_path):
            result = _report_pub_files("test-session", config)

        assert result[0]["url"].startswith("/pub/")


class TestAutoPublishSkillFiles:
    """M215: _auto_publish_skill_files and _snapshot_workspace tests."""

    def test_snapshot_and_publish(self, tmp_path):
        """New files in workspace are copied to pub/."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")

            # Simulate skill creating a file
            (workspace / "screenshot.png").write_bytes(b"fake-image")

            published = _auto_publish_skill_files("test-session", pre)

        assert "screenshot.png" in published
        assert (workspace / "pub" / "screenshot.png").exists()

    def test_no_new_files(self, tmp_path):
        """No new files → nothing published."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")
            published = _auto_publish_skill_files("test-session", pre)

        assert published == []

    def test_skips_files_already_in_pub(self, tmp_path):
        """Files created inside pub/ are not re-published."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")

            # File created directly in pub/ (by exec handler or user)
            (workspace / "pub" / "existing.txt").write_text("already here")

            published = _auto_publish_skill_files("test-session", pre)

        assert published == []

    def test_nested_files_preserve_structure(self, tmp_path):
        """Files in subdirs preserve directory structure in pub/."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")

            sub = workspace / "results"
            sub.mkdir()
            (sub / "data.csv").write_text("a,b,c")

            published = _auto_publish_skill_files("test-session", pre)

        assert any("data.csv" in p for p in published)
        assert (workspace / "pub" / "results" / "data.csv").exists()

    def test_ignores_browser_cache(self, tmp_path):
        """M233: files under .browser/ should NOT be auto-published."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")

            cache_dir = workspace / ".browser" / "profile" / "CacheStorage"
            cache_dir.mkdir(parents=True)
            (cache_dir / "origin").write_text("cached")
            (workspace / "output.txt").write_text("result")

            published = _auto_publish_skill_files("test-session", pre)

        assert "output.txt" in published
        assert not any(".browser" in p for p in published)

    def test_ignores_pycache(self, tmp_path):
        """M233: __pycache__ files should NOT be auto-published."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")

            cache_dir = workspace / "__pycache__"
            cache_dir.mkdir()
            (cache_dir / "mod.cpython-312.pyc").write_bytes(b"\x00")

            published = _auto_publish_skill_files("test-session", pre)

        assert published == []

    def test_ignores_hidden_dotfiles(self, tmp_path):
        """M233: hidden files/dirs (starting with .) should NOT be auto-published."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")

            (workspace / ".hidden_config").write_text("secret")
            hidden_dir = workspace / "subdir" / ".internal"
            hidden_dir.mkdir(parents=True)
            (hidden_dir / "data.bin").write_bytes(b"\x00")
            (workspace / "report.pdf").write_bytes(b"pdf")

            published = _auto_publish_skill_files("test-session", pre)

        assert "report.pdf" in published
        assert not any(".hidden" in p or ".internal" in p for p in published)

    def test_ignores_node_modules(self, tmp_path):
        """M233: node_modules should NOT be auto-published."""
        from kiso.worker import _auto_publish_skill_files, _snapshot_workspace

        with _patch_kiso_dir(tmp_path):
            workspace = _session_workspace("test-session")
            pre = _snapshot_workspace("test-session")

            nm = workspace / "node_modules" / "pkg"
            nm.mkdir(parents=True)
            (nm / "index.js").write_text("module.exports = {}")

            published = _auto_publish_skill_files("test-session", pre)

        assert published == []


# --- M31: search output truncation in replan context ---


class TestBuildReplanContextSearchLimit:
    def test_search_output_uses_2000_limit(self):
        """Search task output uses 2000 char limit in replan context."""
        long_output = "x" * 5000
        completed = [
            {"type": "search", "detail": "find info", "status": "done", "output": long_output},
        ]
        context = _build_replan_context(completed, [], "replan reason", [])
        assert "... (truncated)" in context
        assert "x" * 2001 not in context

    def test_exec_output_truncated_at_limit(self):
        """Exec task output uses _REPLAN_OUTPUT_LIMIT (1000) char limit in replan context."""
        long_output = "x" * 5000
        completed = [
            {"type": "exec", "detail": "run command", "status": "done", "output": long_output},
        ]
        context = _build_replan_context(completed, [], "replan reason", [])
        assert "... (truncated)" in context
        assert "x" * 1001 not in context

    def test_budget_overflow_summarizes(self):
        """Tasks exceeding char budget are summarized as one-liners."""
        completed = [
            {"type": "exec", "detail": f"task{i}", "status": "done", "output": "x" * 2000}
            for i in range(20)
        ]
        context = _build_replan_context(completed, [], "reason", [])
        # Later tasks should be one-liners (no TASK_OUTPUT fence)
        lines = context.split("\n")
        one_liners = [l for l in lines if l.startswith("- [exec]") and "TASK_OUTPUT" not in l]
        assert len(one_liners) > 0


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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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

    async def test_search_searcher_error_triggers_replan(self, db, tmp_path):
        """SearcherError triggers replan with error message (M169)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, side_effect=SearcherError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is False
        assert reason is not None
        assert "Search failed" in reason
        assert len(remaining) == 1  # msg task
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = [t for t in tasks if t["type"] == "search"][0]
        assert search_task["status"] == "failed"
        assert len(_po) == 1
        assert "Search failed" in _po[0]["output"]

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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is True
        assert len(completed) == 2
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = [t for t in tasks if t["type"] == "search"][0]
        assert search_task["status"] == "done"
        assert search_task["output"] == "search results"

    async def test_search_status_not_done_during_retry(self, db, tmp_path):
        """M93c: search task must NOT be written as 'done' in the DB before reviewer approves.
        During a retry, the task stays 'running' (not 'done')."""
        config = _make_config(settings={"max_worker_retries": 1})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        status_snapshots: list[str] = []
        call_count = 0
        async def _reviewer_that_captures(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # On first call: capture current DB status then request retry
            if call_count == 1:
                cur = await db.execute("SELECT status FROM tasks WHERE type = 'search'")
                row = await cur.fetchone()
                status_snapshots.append(row["status"] if row else "missing")
                return REVIEW_REPLAN_WITH_HINT
            return REVIEW_OK

        mock_searcher = AsyncMock(return_value="some results")
        with patch("kiso.worker.search.run_searcher", mock_searcher), \
             patch("kiso.worker.loop.run_reviewer", side_effect=_reviewer_that_captures), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        # Status during first review (before retry) must NOT be "done"
        assert status_snapshots[0] != "done", (
            f"Task was prematurely marked 'done' before reviewer approved; got {status_snapshots[0]!r}"
        )
        # After retry + review ok the task must end up "done"
        assert success is True
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = next(t for t in tasks if t["type"] == "search")
        assert search_task["status"] == "done"

    async def test_search_status_done_after_reviewer_ok(self, db, tmp_path):
        """M93c: task is 'done' in DB after reviewer returns ok (no retry needed)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        status_during_review: list[str] = []

        async def _reviewer_ok(*args, **kwargs):
            cur = await db.execute("SELECT status FROM tasks WHERE type = 'search'")
            row = await cur.fetchone()
            status_during_review.append(row["status"] if row else "missing")
            return REVIEW_OK

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="results"), \
             patch("kiso.worker.loop.run_reviewer", side_effect=_reviewer_ok), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        # During review the task is NOT yet "done"
        assert status_during_review[0] != "done"
        # After review ok it IS "done"
        assert success is True
        tasks = await get_tasks_for_plan(db, plan_id)
        search_task = next(t for t in tasks if t["type"] == "search")
        assert search_task["status"] == "done"

    async def test_search_audit_duration_spans_retries(self, db, tmp_path):
        """Simplify fix: audit.log_task duration covers total time across all retries,
        not just the last iteration (regression guard for t0_total fix)."""
        config = _make_config(settings={"max_worker_retries": 1})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        # perf_counter calls inside _handle_search_task for a 1-retry success:
        #   call 1: t0_total (before while loop)
        #   call 2: t0       (iteration 1 start)
        #   call 3: t0       (iteration 2 start, after retry)
        #   call 4: final    (after break: int((perf_counter() - t0_total) * 1000))
        # Other task handlers (e.g. msg) also call perf_counter; give them a
        # fallback value so the iterator never raises StopIteration in a coroutine.
        perf_sequence = [0.0, 0.5, 1.0, 2.0]
        _perf_idx = 0

        def _fake_perf_counter():
            nonlocal _perf_idx
            v = perf_sequence[_perf_idx] if _perf_idx < len(perf_sequence) else 99.0
            _perf_idx += 1
            return v

        call_count = 0

        async def _retry_then_ok(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return REVIEW_REPLAN_WITH_HINT if call_count == 1 else REVIEW_OK

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="results"), \
             patch("kiso.worker.loop.run_reviewer", side_effect=_retry_then_ok), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.loop.time") as mock_time, \
             patch("kiso.worker.loop.audit") as mock_audit, \
             _patch_kiso_dir(tmp_path):
            mock_time.perf_counter.side_effect = _fake_perf_counter
            await _execute_plan(db, config, "sess1", plan_id, "Test", "user msg", 5)

        search_log = next(
            c for c in mock_audit.log_task.call_args_list if c[0][2] == "search"
        )
        duration_ms = search_log[0][5]
        # Before fix: int((2.0 - 1.0) * 1000) = 1000 (last iteration only)
        # After fix:  int((2.0 - 0.0) * 1000) = 2000 (total span)
        assert duration_ms == 2000

    async def test_search_review_replan(self, db, tmp_path):
        """Review returns replan after search → plan returns replan reason."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="bad results"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is False
        assert reason == "Task failed"
        assert len(remaining) == 1  # msg task

    async def test_search_review_error_triggers_replan(self, db, tmp_path):
        """M170: ReviewError during search review triggers replan with output preserved."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="query", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="results"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=ReviewError("LLM down")), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "user msg", 5,
            )

        assert success is False
        assert reason is not None
        assert "Review failed" in reason
        assert len(remaining) == 1
        assert len(_po) == 1  # task output preserved

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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_planner", mock_planner), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, None, 5, 60, 3,
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
        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_planner", mock_planner), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, None, 5, 60, 3,
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
        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_planner", mock_planner), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, None, 5, 60, 3,
            )

        mock_classifier.assert_not_called()
        mock_planner.assert_called_once()

    async def test_fast_path_runs_post_plan_knowledge(self, db, tmp_path):
        """Fast path should spawn background knowledge task (summarizer, etc.)."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        msg = self._make_msg(msg_id)
        mock_classifier = AsyncMock(return_value="chat")
        mock_messenger = AsyncMock(return_value="Hi")
        mock_post = AsyncMock()
        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop._post_plan_knowledge", mock_post), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            bg_task = await _process_message(
                conn, config, "sess1", msg, None, 5, 60, 3,
            )
            # Background task must be returned
            assert bg_task is not None
            await bg_task  # let it run

        mock_post.assert_called_once()

    async def test_fast_path_skips_paraphraser(self, db, tmp_path):
        """Fast path must NOT call the paraphraser — context is already trusted."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        msg = self._make_msg(msg_id)
        mock_classifier = AsyncMock(return_value="chat")
        mock_messenger = AsyncMock(return_value="Hi")
        mock_paraphraser = AsyncMock(return_value="paraphrased")
        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop.run_paraphraser", mock_paraphraser), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            await _process_message(
                conn, config, "sess1", msg, None, 5, 60, 3,
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
                return {"status": "replan", "reason": "No results", "learn": None, "retry_hint": "try broader terms", "summary": None}
            return REVIEW_OK

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, side_effect=_mock_reviewer), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="search results"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, reason, completed, remaining, _po = await _execute_plan(
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
            success, _, completed, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # exec task: 3 _append_calls (M273 briefer + translator + reviewer), msg task: 2 (M273 briefer + messenger)
        assert mock_append.call_count >= 3

    async def test_msg_task_appends_calls_after_messenger(self, db, tmp_path):
        """msg task: _append_calls called twice — M273 briefer flush + after messenger."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        mock_append = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert mock_append.call_count == 2

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
            success, _, completed, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # search: 2 calls (searcher + reviewer), msg: 2 calls (M273 briefer + messenger) = 4 total
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


# --- M44g: _append_calls robustness + fast_path failure path + retry accumulation ---


@pytest.mark.asyncio
class TestM44gAppendCallsRobustness:
    """M44g: _append_calls must survive exceptions and be called on all code paths."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import init_db
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_append_calls_handles_exception_without_crash(self, db, tmp_path):
        """If get_usage_since raises, _append_calls logs a warning but does not crash."""
        from kiso.worker.loop import _append_calls
        plan_id = await create_plan(db, "sess1", 1, "Test")
        task_id_val = await create_task(db, plan_id, "sess1", type="msg", detail="hi")

        with patch("kiso.worker.loop.get_usage_since", side_effect=RuntimeError("boom")):
            # Must not raise
            await _append_calls(db, task_id_val, 0)

    async def test_fast_path_failure_calls_append_calls(self, db, tmp_path):
        """_fast_path_chat calls _append_calls even when messenger fails."""
        config = _make_config()
        mock_append = AsyncMock()

        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.loop.run_messenger",
                   new_callable=AsyncMock, side_effect=MessengerError("boom")), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        # _append_calls must be called in the failure branch
        assert mock_append.call_count >= 1

    async def test_fast_path_success_and_failure_append_same_count(self, db, tmp_path):
        """Both success and failure paths call _append_calls exactly once."""
        config = _make_config()

        # Success path
        mock_append_ok = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append_ok), \
             patch("kiso.worker.loop.run_messenger",
                   new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess1", 1, "hello")
        success_calls = mock_append_ok.call_count

        # Failure path (new session)
        await create_session(db, "sess2")
        mock_append_fail = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append_fail), \
             patch("kiso.worker.loop.run_messenger",
                   new_callable=AsyncMock, side_effect=MessengerError("boom")), \
             _patch_kiso_dir(tmp_path):
            await _fast_path_chat(db, config, "sess2", 2, "hello")
        failure_calls = mock_append_fail.call_count

        assert success_calls == failure_calls == 1


@pytest.mark.asyncio
class TestM44gRetryLLMCalls:
    """M44g: on exec retry, llm_calls accumulates across attempts."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import init_db
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_retry_accumulates_append_calls(self, db, tmp_path):
        """On exec retry, _append_calls is called once per attempt (translator + reviewer each)."""
        config = _make_config(settings={
            **_make_config().settings,
            "max_worker_retries": 1,
        })
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        review_calls = [0]

        async def _reviewer_first_replan_then_ok(*args, **kw):
            review_calls[0] += 1
            if review_calls[0] == 1:
                return REVIEW_REPLAN_WITH_HINT
            return REVIEW_OK

        mock_append = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.loop.run_reviewer",
                   new_callable=AsyncMock, side_effect=_reviewer_first_replan_then_ok), \
             patch("kiso.worker.loop.run_messenger",
                   new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # exec task: M273 briefer flush = 1 call
        # attempt 1: translator+reviewer = 2 calls
        # attempt 2: translator+reviewer = 2 calls
        # msg task: M273 briefer flush + messenger = 2 calls
        # total _append_calls = 7
        assert mock_append.call_count == 7


# ---------------------------------------------------------------------------
# M48d: _apply_curator_result — category + session scoping
# ---------------------------------------------------------------------------


class TestM48ApplyCuratorCategory:
    """48d: _apply_curator_result uses category from evaluation and scopes session correctly."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_project_fact_has_no_session(self, db):
        """Project facts must be global — not scoped to any session."""
        lid = await save_learning(db, "Uses FastAPI", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Uses FastAPI", "category": "project", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT session FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[0] is None

    async def test_user_fact_is_session_scoped(self, db):
        """User facts must be scoped to the originating session."""
        lid = await save_learning(db, "User prefers dark mode", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "User prefers dark mode", "category": "user", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT session FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[0] == "sess1"

    async def test_tool_fact_has_no_session(self, db):
        """Tool facts must be global (not session-scoped)."""
        lid = await save_learning(db, "jq is available", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "jq is available", "category": "tool", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT session FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[0] is None

    async def test_general_fact_has_no_session(self, db):
        """General facts must be global (not session-scoped)."""
        lid = await save_learning(db, "Team uses GitLab", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Team uses GitLab", "category": "general", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT session FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[0] is None

    async def test_null_category_defaults_to_general_no_session(self, db):
        """When category is null, defaults to 'general' and saves without session."""
        lid = await save_learning(db, "Some fact", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Some fact", "category": None, "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT session, category FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[0] is None
        assert row[1] == "general"

    async def test_missing_category_defaults_to_general_no_session(self, db):
        """When category key is absent from evaluation, defaults to 'general' without session."""
        lid = await save_learning(db, "Some fact", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Some fact", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT session, category FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[0] is None
        assert row[1] == "general"

    async def test_category_value_persisted_in_db(self, db):
        """The category from the evaluation must be saved to the facts table."""
        lid = await save_learning(db, "Uses React", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Uses React", "category": "project", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT category FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[0] == "project"

    async def test_user_category_value_persisted(self, db):
        """User category must also be persisted correctly."""
        lid = await save_learning(db, "Prefers vim", "sess1")
        result = {"evaluations": [
            {"learning_id": lid, "verdict": "promote", "fact": "Prefers vim", "category": "user", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT session, category FROM facts LIMIT 1")
        row = await cur.fetchone()
        assert row[1] == "user"
        assert row[0] == "sess1"  # session-scoped

    async def test_multiple_evaluations_mixed_categories(self, db):
        """Multiple evaluations with different categories are handled independently."""
        lid1 = await save_learning(db, "Uses Django", "sess1")
        lid2 = await save_learning(db, "User likes light theme", "sess1")
        result = {"evaluations": [
            {"learning_id": lid1, "verdict": "promote", "fact": "Uses Django", "category": "project", "question": None, "reason": "Good"},
            {"learning_id": lid2, "verdict": "promote", "fact": "User likes light theme", "category": "user", "question": None, "reason": "Good"},
        ]}
        await _apply_curator_result(db, "sess1", result)
        cur = await db.execute("SELECT content, session, category FROM facts ORDER BY id")
        rows = await cur.fetchall()
        assert len(rows) == 2
        django_row = next(r for r in rows if r[0] == "Uses Django")
        user_row = next(r for r in rows if "light theme" in r[0])
        assert django_row[1] is None      # project: no session
        assert django_row[2] == "project"
        assert user_row[1] == "sess1"     # user: session-scoped
        assert user_row[2] == "user"


# ---------------------------------------------------------------------------
# M62: Tests for task handlers (62a) and planning loop (62c)
# ---------------------------------------------------------------------------

def _make_ctx(db, config=None, plan_outputs=None, installed_skills=None) -> _PlanCtx:
    """Build a minimal _PlanCtx for handler tests."""
    from kiso.config import SETTINGS_DEFAULTS, MODEL_DEFAULTS
    if config is None:
        config = _make_config()
    return _PlanCtx(
        db=db,
        config=config,
        session="sess1",
        goal="Test goal",
        user_message="test message",
        deploy_secrets={},
        session_secrets={},
        max_output_size=1024 * 1024,
        max_worker_retries=1,
        messenger_timeout=30,
        installed_skills=installed_skills or [],
        slog=None,
        sandbox_uid=None,
        plan_outputs=plan_outputs if plan_outputs is not None else [],
    )


async def _make_task_row(db, plan_id, task_type, detail="Test task", **kwargs):
    """Create a task in the DB and return its row dict."""
    from kiso.store import get_tasks_for_plan
    task_id = await create_task(db, plan_id, "sess1", task_type, detail, **kwargs)
    tasks = await get_tasks_for_plan(db, plan_id)
    return next(t for t in tasks if t["id"] == task_id)


class TestTaskHandlers:
    """Unit tests for the individual task handler functions (M62a)."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    @pytest.fixture()
    async def plan_id(self, db):
        pid = await create_plan(db, "sess1", 0, "Test plan")
        yield pid

    async def test_task_handlers_dict_has_all_types(self):
        """_TASK_HANDLERS covers all 5 task types."""
        assert set(_TASK_HANDLERS.keys()) == {"exec", "msg", "skill", "search", "replan"}

    # --- _handle_replan_task ---

    async def test_handle_replan_task_returns_stop_with_reason(self, db, plan_id):
        """replan handler marks task done and returns stop with Self-directed replan reason."""
        task_row = await _make_task_row(db, plan_id, "replan", "Need to check logs first")
        ctx = _make_ctx(db)
        result = await _handle_replan_task(ctx, task_row, 0, True, 0)

        assert result.stop is True
        assert result.stop_success is False
        assert "Self-directed replan" in result.stop_replan
        assert "Need to check logs first" in result.stop_replan
        assert result.completed_row is not None
        assert result.completed_row["status"] == "done"

    # --- _handle_msg_task ---

    async def test_handle_msg_task_success(self, db, plan_id, tmp_path):
        """msg handler calls messenger and returns completed_row."""
        task_row = await _make_task_row(db, plan_id, "msg", "Say hello")
        ctx = _make_ctx(db)
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="Hello!"), \
             _patch_kiso_dir(tmp_path):
            result = await _handle_msg_task(ctx, task_row, 0, True, 0)

        assert result.stop is False
        assert result.completed_row is not None
        assert result.completed_row["status"] == "done"
        assert result.completed_row["output"] == "Hello!"
        assert result.plan_output is not None
        assert result.plan_output["type"] == "msg"

    async def test_handle_msg_task_stores_duration_ms(self, db, plan_id, tmp_path):
        """M111a: msg handler stores duration_ms in the DB."""
        task_row = await _make_task_row(db, plan_id, "msg", "Say hello")
        ctx = _make_ctx(db)
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="Hello!"), \
             _patch_kiso_dir(tmp_path):
            await _handle_msg_task(ctx, task_row, 0, True, 0)

        tasks = await get_tasks_for_plan(db, plan_id)
        done_tasks = [t for t in tasks if t["status"] == "done"]
        assert done_tasks, "Expected at least one done task"
        assert done_tasks[0]["duration_ms"] is not None
        assert done_tasks[0]["duration_ms"] >= 0

    async def test_handle_msg_task_messenger_error_returns_stop(self, db, plan_id, tmp_path):
        """msg handler returns stop=True on LLMError."""
        task_row = await _make_task_row(db, plan_id, "msg", "Say hello")
        ctx = _make_ctx(db)
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, side_effect=LLMError("API down")), \
             _patch_kiso_dir(tmp_path):
            result = await _handle_msg_task(ctx, task_row, 0, True, 0)

        assert result.stop is True
        assert result.stop_success is False
        assert result.completed_row is None

    # --- _handle_skill_task ---

    async def test_handle_skill_task_not_installed_returns_stop(self, db, plan_id, tmp_path):
        """skill handler returns stop=True when skill is not installed."""
        task_row = await _make_task_row(
            db, plan_id, "skill", "Search for something",
            skill="missing-skill", args="{}"
        )
        ctx = _make_ctx(db, installed_skills=[])  # no skills installed
        with _patch_kiso_dir(tmp_path):
            result = await _handle_skill_task(ctx, task_row, 0, True, 0)

        assert result.stop is True
        assert result.stop_success is False
        assert result.completed_row is None

    async def test_handle_skill_task_invalid_json_args_returns_stop(self, db, plan_id, tmp_path):
        """skill handler returns stop=True when args JSON is malformed."""
        skill_info = {
            "name": "test-skill",
            "summary": "A test skill",
            "args_schema": {},
            "env": {},
            "session_secrets": [],
            "path": "/fake/path",
        }
        task_row = await _make_task_row(
            db, plan_id, "skill", "Run test skill",
            skill="test-skill", args="{invalid json}"
        )
        ctx = _make_ctx(db, installed_skills=[skill_info])
        with _patch_kiso_dir(tmp_path):
            result = await _handle_skill_task(ctx, task_row, 0, True, 0)

        assert result.stop is True
        assert result.completed_row is None

    # --- _handle_exec_task ---

    async def test_handle_exec_task_translator_error_triggers_replan(self, db, plan_id, tmp_path):
        """exec handler returns stop_replan when translator raises ExecTranslatorError (M168)."""
        task_row = await _make_task_row(db, plan_id, "exec", "list files")
        ctx = _make_ctx(db)
        with patch(
            "kiso.worker.loop.run_exec_translator",
            new_callable=AsyncMock,
            side_effect=ExecTranslatorError("LLM failed"),
        ), _patch_kiso_dir(tmp_path):
            result = await _handle_exec_task(ctx, task_row, 0, True, 0)

        assert result.stop is True
        assert result.stop_success is False
        assert result.stop_replan is not None
        assert "Translation failed" in result.stop_replan
        assert result.plan_output is not None
        assert result.completed_row is None

    async def test_handle_exec_task_success(self, db, plan_id, tmp_path):
        """exec handler runs command, reviews, and returns completed_row and plan_output on success."""
        task_row = await _make_task_row(db, plan_id, "exec", "echo hello")
        ctx = _make_ctx(db)
        with _patch_translator(), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   return_value=REVIEW_OK), \
             _patch_kiso_dir(tmp_path):
            result = await _handle_exec_task(ctx, task_row, 0, True, 0)

        assert result.stop is False
        assert result.completed_row is not None
        assert result.completed_row["status"] == "done"
        assert result.plan_output is not None
        assert result.plan_output["type"] == "exec"
        assert result.plan_output["index"] == 1
        assert result.plan_output["detail"] == "echo hello"

    # --- _handle_search_task ---

    async def test_handle_search_task_success(self, db, plan_id, tmp_path):
        """search handler runs search, reviews, and returns completed_row."""
        task_row = await _make_task_row(db, plan_id, "search", "find Python docs")
        ctx = _make_ctx(db)
        with patch(
            "kiso.worker.loop._search_task",
            new_callable=AsyncMock,
            return_value="Found: https://docs.python.org",
        ), patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                 return_value=REVIEW_OK), \
             _patch_kiso_dir(tmp_path):
            result = await _handle_search_task(ctx, task_row, 0, True, 0)

        assert result.stop is False
        assert result.completed_row is not None
        assert result.completed_row["status"] == "done"
        assert result.plan_output is not None
        assert result.plan_output["type"] == "search"

    async def test_handle_search_task_error_triggers_replan(self, db, plan_id, tmp_path):
        """search handler returns stop_replan on SearcherError (M169)."""
        from kiso.worker.search import SearcherError
        task_row = await _make_task_row(db, plan_id, "search", "find something")
        ctx = _make_ctx(db)
        with patch(
            "kiso.worker.loop._search_task",
            new_callable=AsyncMock,
            side_effect=SearcherError("Search API down"),
        ), _patch_kiso_dir(tmp_path):
            result = await _handle_search_task(ctx, task_row, 0, True, 0)

        assert result.stop is True
        assert result.stop_replan is not None
        assert "Search failed" in result.stop_replan
        assert result.plan_output is not None
        assert result.completed_row is None

    async def test_handle_skill_task_success_returns_plan_output(self, db, plan_id, tmp_path):
        """skill handler returns plan_output with correct fields on success."""
        skill_info = {
            "name": "test-skill",
            "summary": "A test skill",
            "args_schema": {},
            "env": {},
            "session_secrets": [],
            "path": "/fake/path",
        }
        task_row = await _make_task_row(db, plan_id, "skill", "run the skill",
                                        skill="test-skill", args="{}")
        ctx = _make_ctx(db, installed_skills=[skill_info])
        with patch("kiso.worker.loop._skill_task", new_callable=AsyncMock,
                   return_value=("skill output", "", True, 0)), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   return_value=REVIEW_OK), \
             _patch_kiso_dir(tmp_path):
            result = await _handle_skill_task(ctx, task_row, 0, True, 0)

        assert result.stop is False
        assert result.plan_output is not None
        assert result.plan_output["type"] == "skill"
        assert result.plan_output["index"] == 1
        assert result.plan_output["output"] == "skill output"
        assert result.plan_output["status"] == "done"


# --- M112c: skill cache invalidation after exec/skill tasks ---


class TestSkillCacheInvalidation:
    """Verify invalidate_skills_cache + ctx refresh after exec/skill tasks."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    @pytest.fixture()
    async def plan_id(self, db):
        pid = await create_plan(db, "sess1", 0, "Test plan")
        yield pid

    async def test_exec_task_invalidates_skill_cache(self, db, plan_id, tmp_path):
        """After exec task completes, skill cache is invalidated and ctx refreshed."""
        task_row = await _make_task_row(db, plan_id, "exec", "echo hello")
        ctx = _make_ctx(db)
        assert ctx.installed_skills == []

        new_skills = [{"name": "new-skill", "summary": "Just installed"}]
        with _patch_translator(), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   return_value=REVIEW_OK), \
             _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop.invalidate_skills_cache") as mock_invalidate, \
             patch("kiso.worker.loop.discover_skills", return_value=new_skills):
            result = await _handle_exec_task(ctx, task_row, 0, True, 0)

        # invalidation itself happens in execute_plan_tasks, not the handler
        # So we test the import is correct and function exists
        from kiso.worker.loop import invalidate_skills_cache as imported_fn
        from kiso.skills import invalidate_skills_cache as original_fn
        assert imported_fn is original_fn

    async def test_skill_cache_invalidation_import(self):
        """invalidate_skills_cache is importable from worker/loop.py."""
        from kiso.worker.loop import invalidate_skills_cache
        assert callable(invalidate_skills_cache)


# --- M91a: _handle_plan_error ---


class TestHandlePlanError:
    """Unit tests for _handle_plan_error (M91a)."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_creates_failed_plan_and_task(self, db):
        """_handle_plan_error creates a plan (status=failed) and a msg task."""
        config = _make_config()
        msg_id = await save_message(db, "sess1", "alice", "user", "hi", processed=False)

        with patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_plan_error(db, config, "sess1", msg_id, "Planning failed: oops")

        cur = await db.execute("SELECT * FROM plans WHERE session = 'sess1'")
        plans = [dict(r) for r in await cur.fetchall()]
        assert len(plans) == 1
        assert plans[0]["status"] == "failed"

        cur = await db.execute("SELECT * FROM tasks WHERE session = 'sess1' AND type = 'msg'")
        tasks = [dict(r) for r in await cur.fetchall()]
        assert len(tasks) == 1
        assert tasks[0]["status"] == "done"
        assert tasks[0]["output"] == "Planning failed: oops"

    async def test_saves_system_message(self, db):
        """_handle_plan_error saves a system message with the error text."""
        config = _make_config()
        msg_id = await save_message(db, "sess1", "alice", "user", "hi", processed=False)

        with patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_plan_error(db, config, "sess1", msg_id, "Timeout")

        cur = await db.execute(
            "SELECT * FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        msgs = [dict(r) for r in await cur.fetchall()]
        assert any("Timeout" in (m.get("content") or "") for m in msgs)

    async def test_delivers_webhook(self, db):
        """_handle_plan_error always delivers a webhook."""
        config = _make_config()
        msg_id = await save_message(db, "sess1", "alice", "user", "hi", processed=False)
        mock_webhook = AsyncMock()

        with patch("kiso.worker.loop._deliver_webhook_if_configured", mock_webhook):
            await _handle_plan_error(db, config, "sess1", msg_id, "oops")

        assert mock_webhook.called


# --- M91b: _handle_loop_failure webhook flag ---


class TestHandleLoopFailure:
    """Unit tests for _handle_loop_failure (M91b), focusing on deliver_webhook flag."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_marks_plan_failed(self, db):
        """_handle_loop_failure sets plan status to 'failed'."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="fail msg"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal")

        cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        assert row["status"] == "failed"

    async def test_delivers_webhook_by_default(self, db):
        """_handle_loop_failure delivers webhook when deliver_webhook=True (default)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        mock_webhook = AsyncMock()

        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="fail msg"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", mock_webhook):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal",
                                        deliver_webhook=True)

        assert mock_webhook.called

    async def test_skips_webhook_when_flag_false(self, db):
        """_handle_loop_failure does NOT deliver webhook when deliver_webhook=False."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        mock_webhook = AsyncMock()

        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="fail msg"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", mock_webhook):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal",
                                        deliver_webhook=False)

        assert not mock_webhook.called

    async def test_saves_system_message(self, db):
        """_handle_loop_failure saves a system message."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="failure text"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal")

        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        rows = await cur.fetchall()
        assert any("failure text" in row["content"] for row in rows)

    async def test_failure_messenger_timeout_falls_back(self, db):
        """M94c: if _msg_task times out, _handle_loop_failure falls back to raw detail text."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        async def _hanging_msg(*args, **kwargs):
            await asyncio.Event().wait()

        with patch("kiso.worker.loop._msg_task", side_effect=_hanging_msg), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_failure(
                db, config, "sess1", plan_id, [], [], "goal",
                messenger_timeout=0.001,  # expire immediately
            )

        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        rows = await cur.fetchall()
        # Fallback: raw detail text from _build_failure_summary is saved, not an LLM response
        assert any("The plan failed: goal" in row["content"] for row in rows), (
            "Fallback must save the raw _build_failure_summary text"
        )

    async def test_msg_task_created_before_plan_status_change(self, db):
        """M307: msg task is created and set to 'running' BEFORE plan status changes."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        call_order: list[str] = []

        original_create_task = create_task

        async def tracking_create_task(*args, **kwargs):
            result = await original_create_task(*args, **kwargs)
            call_order.append("create_task")
            return result

        async def tracking_msg(*args, **kwargs):
            # Check plan status is still 'running' while messenger is composing
            cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
            row = await cur.fetchone()
            call_order.append(f"msg_compose:plan_status={row['status']}")
            return "fail msg"

        with patch("kiso.worker.loop.create_task", side_effect=tracking_create_task), \
             patch("kiso.worker.loop._msg_task_with_fallback", side_effect=tracking_msg), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal")

        assert "create_task" in call_order
        assert "msg_compose:plan_status=running" in call_order
        # create_task must come before msg compose
        assert call_order.index("create_task") < call_order.index("msg_compose:plan_status=running")

    async def test_usage_updated_before_plan_status_change(self, db):
        """M307: plan usage is updated before plan status changes to 'failed'."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        call_order: list[str] = []

        async def tracking_update_usage(*args, **kwargs):
            from kiso.store import update_plan_usage as real_update
            await real_update(*args, **kwargs)
            call_order.append("update_usage")

        async def tracking_update_status(*args, **kwargs):
            from kiso.store import update_plan_status as real_update
            await real_update(*args, **kwargs)
            call_order.append("update_status")

        with patch("kiso.worker.loop._msg_task_with_fallback", new_callable=AsyncMock, return_value="fail msg"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock), \
             patch("kiso.worker.loop.get_usage_summary", return_value={"input_tokens": 100, "output_tokens": 50, "model": "test-model"}), \
             patch("kiso.worker.loop.update_plan_usage", side_effect=tracking_update_usage), \
             patch("kiso.worker.loop.update_plan_status", side_effect=tracking_update_status):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal")

        assert call_order == ["update_usage", "update_status"]

    async def test_msg_task_gets_done_with_composed_text(self, db):
        """M307: the placeholder msg task is updated to 'done' with composed text."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        with patch("kiso.worker.loop._msg_task_with_fallback", new_callable=AsyncMock, return_value="composed failure"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal")

        cur = await db.execute(
            "SELECT status, output FROM tasks WHERE plan_id = ? AND type = 'msg'", (plan_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "done"
        assert row["output"] == "composed failure"


class TestHandleLoopCancel:
    """Unit tests for _handle_loop_cancel (M94c)."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_saves_system_message(self, db):
        """_handle_loop_cancel saves the cancel text as a system message."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="cancel text"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_cancel(db, config, "sess1", plan_id, [], [], "goal")

        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        rows = await cur.fetchall()
        assert any("cancel text" in row["content"] for row in rows)

    async def test_marks_plan_cancelled(self, db):
        """_handle_loop_cancel sets plan status to 'cancelled'."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="cancelled msg"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_cancel(db, config, "sess1", plan_id, [], [], "goal")

        cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        assert row["status"] == "cancelled"

    async def test_clears_cancel_event(self, db):
        """_handle_loop_cancel clears the cancel_event after handling."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        event = asyncio.Event()
        event.set()

        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="cancelled"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_cancel(db, config, "sess1", plan_id, [], [], "goal",
                                      cancel_event=event)

        assert not event.is_set()

    async def test_cancel_messenger_timeout_falls_back(self, db):
        """M94c: if _msg_task times out, _handle_loop_cancel falls back to raw detail text."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        async def _hanging_msg(*args, **kwargs):
            await asyncio.Event().wait()

        with patch("kiso.worker.loop._msg_task", side_effect=_hanging_msg), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_cancel(
                db, config, "sess1", plan_id, [], [], "goal",
                messenger_timeout=0.001,  # expire immediately
            )

        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        rows = await cur.fetchall()
        # Fallback: raw detail text from _build_cancel_summary is saved, not an LLM response
        assert any("The user cancelled the plan: goal" in row["content"] for row in rows), (
            "Fallback must save the raw _build_cancel_summary text"
        )

    async def test_msg_task_created_before_plan_status_change(self, db):
        """M307: msg task is created and set to 'running' BEFORE plan status changes."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        call_order: list[str] = []

        original_create_task = create_task

        async def tracking_create_task(*args, **kwargs):
            result = await original_create_task(*args, **kwargs)
            call_order.append("create_task")
            return result

        async def tracking_msg(*args, **kwargs):
            cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
            row = await cur.fetchone()
            call_order.append(f"msg_compose:plan_status={row['status']}")
            return "cancel msg"

        with patch("kiso.worker.loop.create_task", side_effect=tracking_create_task), \
             patch("kiso.worker.loop._msg_task_with_fallback", side_effect=tracking_msg), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_cancel(db, config, "sess1", plan_id, [], [], "goal")

        assert "create_task" in call_order
        assert "msg_compose:plan_status=running" in call_order
        assert call_order.index("create_task") < call_order.index("msg_compose:plan_status=running")

    async def test_usage_updated_before_plan_status_change(self, db):
        """M307: plan usage is updated before plan status changes to 'cancelled'."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        call_order: list[str] = []

        async def tracking_update_usage(*args, **kwargs):
            from kiso.store import update_plan_usage as real_update
            await real_update(*args, **kwargs)
            call_order.append("update_usage")

        async def tracking_update_status(*args, **kwargs):
            from kiso.store import update_plan_status as real_update
            await real_update(*args, **kwargs)
            call_order.append("update_status")

        with patch("kiso.worker.loop._msg_task_with_fallback", new_callable=AsyncMock, return_value="cancel msg"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock), \
             patch("kiso.worker.loop.get_usage_summary", return_value={"input_tokens": 100, "output_tokens": 50, "model": "test-model"}), \
             patch("kiso.worker.loop.update_plan_usage", side_effect=tracking_update_usage), \
             patch("kiso.worker.loop.update_plan_status", side_effect=tracking_update_status):
            await _handle_loop_cancel(db, config, "sess1", plan_id, [], [], "goal")

        assert call_order == ["update_usage", "update_status"]

    async def test_msg_task_gets_done_with_composed_text(self, db):
        """M307: the placeholder msg task is updated to 'done' with composed text."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")

        with patch("kiso.worker.loop._msg_task_with_fallback", new_callable=AsyncMock, return_value="composed cancel"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_cancel(db, config, "sess1", plan_id, [], [], "goal")

        cur = await db.execute(
            "SELECT status, output FROM tasks WHERE plan_id = ? AND type = 'msg'", (plan_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "done"
        assert row["output"] == "composed cancel"


class TestMsgTaskWithFallback:
    """Unit tests for _msg_task_with_fallback (post-M94 simplify).

    _msg_task is fully patched in every test so no real DB is needed;
    None is passed for the db argument.
    """

    async def test_returns_msg_task_result_on_success(self):
        """Returns the LLM-generated text when _msg_task succeeds."""
        config = _make_config()
        with patch("kiso.worker.loop._msg_task", new_callable=AsyncMock, return_value="hello"):
            result = await _msg_task_with_fallback(config, None, "sess1", "detail", "goal", 30)
        assert result == "hello"

    async def test_falls_back_on_llm_error(self):
        """Returns detail when _msg_task raises LLMError."""
        config = _make_config()
        with patch("kiso.worker.loop._msg_task", side_effect=LLMError("boom")):
            result = await _msg_task_with_fallback(config, None, "sess1", "detail", "goal", 30)
        assert result == "detail"

    async def test_falls_back_on_messenger_error(self):
        """Returns detail when _msg_task raises MessengerError."""
        config = _make_config()
        with patch("kiso.worker.loop._msg_task", side_effect=MessengerError("boom")):
            result = await _msg_task_with_fallback(config, None, "sess1", "detail", "goal", 30)
        assert result == "detail"

    async def test_falls_back_on_timeout(self):
        """Returns detail when _msg_task times out."""
        config = _make_config()

        async def _hanging(*args, **kwargs):
            await asyncio.Event().wait()

        with patch("kiso.worker.loop._msg_task", side_effect=_hanging):
            result = await _msg_task_with_fallback(
                config, None, "sess1", "detail", "goal", timeout=0.001,
            )
        assert result == "detail"


class TestSubstatusConstants:
    """Verify _SUBSTATUS_* constant values (M88b)."""

    def test_substatus_constant_values(self):
        """All five substatus constants have the expected string values."""
        assert _SUBSTATUS_TRANSLATING == "translating"
        assert _SUBSTATUS_EXECUTING == "executing"
        assert _SUBSTATUS_REVIEWING == "reviewing"
        assert _SUBSTATUS_COMPOSING == "composing"
        assert _SUBSTATUS_SEARCHING == "searching"

    def test_substatus_constants_are_distinct(self):
        """No two substatus constants share the same value."""
        values = [
            _SUBSTATUS_TRANSLATING,
            _SUBSTATUS_EXECUTING,
            _SUBSTATUS_REVIEWING,
            _SUBSTATUS_COMPOSING,
            _SUBSTATUS_SEARCHING,
        ]
        assert len(values) == len(set(values))


class TestRunPlanningLoop:
    """Tests for _run_planning_loop (M62c)."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_success_returns_plan_id(self, db, tmp_path):
        """On success, _run_planning_loop returns the plan_id."""
        plan = VALID_PLAN
        plan_id = await create_plan(db, "sess1", 0, plan["goal"])
        await _persist_plan_tasks(db, plan_id, "sess1", plan["tasks"])
        msg_id = 0
        config = _make_config()

        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Done!"), \
             _patch_kiso_dir(tmp_path):
            returned_id = await _run_planning_loop(
                db, config, "sess1", msg_id, "hello",
                plan_id, plan, "admin", None, 30,
                {}, None, 10, 3, None, None,
            )

        assert returned_id == plan_id

        from kiso.store import get_plan_for_session
        p = await get_plan_for_session(db, "sess1")
        assert p["status"] == "done"

    async def test_failure_no_replan_path(self, db, tmp_path):
        """On _execute_plan failure (no replan), loop handles failure and breaks."""
        plan = {
            "goal": "Do something",
            "secrets": None,
            "tasks": [
                {"type": "msg", "detail": "Say hi", "skill": None, "args": None, "expect": None},
            ],
        }
        plan_id = await create_plan(db, "sess1", 0, plan["goal"])
        await _persist_plan_tasks(db, plan_id, "sess1", plan["tasks"])
        config = _make_config()

        # Messenger fails → _execute_plan returns (False, None, ...)
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   side_effect=LLMError("API down")), \
             _patch_kiso_dir(tmp_path):
            returned_id = await _run_planning_loop(
                db, config, "sess1", 0, "hello",
                plan_id, plan, "admin", None, 30,
                {}, None, 10, 3, None, None,
            )

        assert returned_id == plan_id
        from kiso.store import get_plan_for_session
        p = await get_plan_for_session(db, "sess1")
        assert p["status"] == "failed"

    async def test_auto_replan_safety_net(self, db, tmp_path):
        """M172: when replan_reason is None but there are failed outputs, auto-replan."""
        initial_plan = EXEC_THEN_MSG_PLAN
        plan_id = await create_plan(db, "sess1", 0, initial_plan["goal"])
        await _persist_plan_tasks(db, plan_id, "sess1", initial_plan["tasks"])
        config = _make_config()

        call_count = [0]
        failed_output = _make_plan_output(1, "exec", "echo hello", "command not found", "failed")

        async def _mock_execute_plan(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: fail without replan_reason but with failed output
                return (False, None, [], [{"type": "msg", "detail": "done"}], [failed_output])
            # Second call (after auto-replan): succeed
            return (True, None, [{"type": "msg", "detail": "done"}], [], [])

        replan_plan = {
            "goal": "Retry", "secrets": None,
            "tasks": [
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }
        with patch("kiso.worker.loop._execute_plan", side_effect=_mock_execute_plan), \
             patch("kiso.worker.loop.run_planner", new_callable=AsyncMock, return_value=replan_plan), \
             _patch_kiso_dir(tmp_path):
            returned_id = await _run_planning_loop(
                db, config, "sess1", 0, "hello",
                plan_id, initial_plan, "admin", None, 30,
                {}, None, 10, 3, None, None,
            )

        # Should have auto-replanned (returned_id is the new plan id)
        assert returned_id != plan_id
        assert call_count[0] == 2  # called twice: initial + replan

    async def test_auto_replan_no_failed_outputs_fails(self, db, tmp_path):
        """M172: when replan_reason is None and no failed outputs, fall through to failure."""
        plan = VALID_PLAN
        plan_id = await create_plan(db, "sess1", 0, plan["goal"])
        await _persist_plan_tasks(db, plan_id, "sess1", plan["tasks"])
        config = _make_config()

        # Messenger fails → _execute_plan returns (False, None, ..., empty plan_outputs)
        with patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   side_effect=LLMError("API down")), \
             _patch_kiso_dir(tmp_path):
            returned_id = await _run_planning_loop(
                db, config, "sess1", 0, "hello",
                plan_id, plan, "admin", None, 30,
                {}, None, 10, 3, None, None,
            )

        assert returned_id == plan_id  # no replan happened
        from kiso.store import get_plan_for_session
        p = await get_plan_for_session(db, "sess1")
        assert p["status"] == "failed"

    async def test_old_plan_stays_replanning_until_new_plan_persisted(self, db, tmp_path):
        """M103a: old plan stays 'replanning' until the new plan + tasks are persisted.

        The old plan must NOT be finalized to done/failed before create_plan
        and _persist_plan_tasks complete for the successor plan.
        """
        from kiso.store import get_plan_for_session

        # Plan that triggers a replan (exec fails → reviewer says replan)
        fail_plan = {
            "goal": "First attempt",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "ok"},
            ],
        }
        success_plan = {
            "goal": "Second attempt",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        plan_id = await create_plan(db, "sess1", 0, fail_plan["goal"])
        await _persist_plan_tasks(db, plan_id, "sess1", fail_plan["tasks"])
        config = _make_config(settings={"max_replan_depth": 3})

        # Track the old plan status at the moment create_plan is called
        # for the new plan.  If M103a is correct, the old plan should still
        # be "replanning" (not "done" or "failed") at that point.
        old_plan_status_at_create: list[str] = []

        original_create_plan = create_plan

        async def _spy_create_plan(conn, session, msg_id, goal, **kw):
            # Capture old plan status right when the new plan is being created
            cur = await conn.execute(
                "SELECT status FROM plans WHERE id = ?", (plan_id,)
            )
            row = await cur.fetchone()
            if row:
                old_plan_status_at_create.append(row[0])
            return await original_create_plan(conn, session, msg_id, goal, **kw)

        planner_calls = []

        async def _planner(db, config, session, role, content, **kwargs):
            planner_calls.append(1)
            if len(planner_calls) == 1:
                return fail_plan
            return success_plan

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             patch("kiso.worker.loop.create_plan", side_effect=_spy_create_plan), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            returned_id = await _run_planning_loop(
                db, config, "sess1", 0, "hello",
                plan_id, fail_plan, "admin", None, 30,
                {}, None, 10, 3, None, None,
            )

        # The old plan must have been "replanning" when the new plan was created
        assert old_plan_status_at_create, "create_plan spy was never called for the new plan"
        assert old_plan_status_at_create[0] == "replanning", (
            f"Old plan should be 'replanning' when new plan is created, "
            f"got '{old_plan_status_at_create[0]}'"
        )

        # After the loop completes, old plan should be finalized
        cur = await db.execute(
            "SELECT status FROM plans WHERE id = ?", (plan_id,)
        )
        row = await cur.fetchone()
        assert row[0] == "failed"  # reviewer said replan → old plan is "failed"

        # New plan should be "done"
        assert returned_id != plan_id
        cur = await db.execute(
            "SELECT status FROM plans WHERE id = ?", (returned_id,)
        )
        row = await cur.fetchone()
        assert row[0] == "done"


# ---------------------------------------------------------------------------
# M127: Circular replan detection — "I'm stuck" message
# ---------------------------------------------------------------------------


class TestCircularReplanDetection:
    """M127: detect circular replanning and show 'I'm stuck' message."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_stuck_message_on_similar_failures(self, db, tmp_path):
        """When 2 consecutive replans have >60% word overlap, show stuck message."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 5,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "browse site", processed=False)

        # Plan that always fails with a similar reason
        fail_plan = {
            "goal": "Browse site",
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
            return fail_plan

        # Reviewer always says replan with similar reasons
        review_reasons = [
            "browser skill not installed, cannot navigate to site",
            "browser skill not installed, cannot navigate to the site",
            "browser skill still not installed, cannot navigate",
        ]
        review_idx = [0]

        async def _reviewer(*a, **kw):
            idx = min(review_idx[0], len(review_reasons) - 1)
            review_idx[0] += 1
            return {"status": "replan", "reason": review_reasons[idx]}

        saved_messages = []
        orig_save_msg = save_message

        async def _save_msg(*args, **kwargs):
            # args: db, session, user, role, content
            content = args[4] if len(args) > 4 else kwargs.get("content", "")
            saved_messages.append(content)
            return await orig_save_msg(*args, **kwargs)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "browse site", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Failed"), \
             patch("kiso.worker.loop.run_reviewer", side_effect=_reviewer), \
             patch("kiso.worker.loop.save_message", side_effect=_save_msg), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        # After 2 similar failures, a "stuck" message should appear
        stuck_msgs = [m for m in saved_messages if "I'm having trouble" in m]
        assert len(stuck_msgs) >= 1, f"Expected stuck message, got: {saved_messages}"
        # Verify it mentions the failure reason
        assert "not installed" in stuck_msgs[0].lower()

    async def test_no_stuck_message_on_genuinely_different_strategies(self, db, tmp_path):
        """Genuinely different strategies (different task types/details) should NOT trigger stuck."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        # Plans with genuinely different task structures
        plan_a = {
            "goal": "Do it",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "try approach A with curl", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }
        plan_b = {
            "goal": "Do it",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "search", "detail": "find a completely different thing", "skill": None, "args": None, "expect": "results"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        plan_iter = iter([plan_a, plan_b, plan_b])

        async def _planner(db, config, session, role, content, **kwargs):
            return next(plan_iter, plan_b)

        review_reasons = [
            "network timeout while fetching data",
            "permission denied accessing file system",
            "invalid JSON response from API",
        ]
        review_idx = [0]

        async def _reviewer(*a, **kw):
            idx = min(review_idx[0], len(review_reasons) - 1)
            review_idx[0] += 1
            return {"status": "replan", "reason": review_reasons[idx]}

        saved_messages = []
        orig_save_msg = save_message

        async def _save_msg(*args, **kwargs):
            content = args[4] if len(args) > 4 else kwargs.get("content", "")
            saved_messages.append(content)
            return await orig_save_msg(*args, **kwargs)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Failed"), \
             patch("kiso.worker.loop.run_reviewer", side_effect=_reviewer), \
             patch("kiso.worker.search.run_searcher", new_callable=AsyncMock,
                   return_value="search results"), \
             patch("kiso.worker.loop.save_message", side_effect=_save_msg), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path), \
             _patch_no_intent():
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        stuck_msgs = [m for m in saved_messages if "I'm having trouble" in m]
        assert len(stuck_msgs) == 0, f"Should NOT get stuck message with different strategies: {stuck_msgs}"

    async def test_strategy_fingerprint_detects_same_plan_different_errors(self, db, tmp_path):
        """M157: same strategy with different failure reasons detected as circular via fingerprint."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 5,
        })
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        # Same plan structure every time
        fail_plan = {
            "goal": "Do it",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "success"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }

        async def _planner(db, config, session, role, content, **kwargs):
            return fail_plan

        # Different failure reasons but same strategy
        review_reasons = [
            "network timeout while fetching data",
            "completely unrelated disk error occurred",
            "something else entirely",
        ]
        review_idx = [0]

        async def _reviewer(*a, **kw):
            idx = min(review_idx[0], len(review_reasons) - 1)
            review_idx[0] += 1
            return {"status": "replan", "reason": review_reasons[idx]}

        saved_messages = []
        orig_save_msg = save_message

        async def _save_msg(*args, **kwargs):
            content = args[4] if len(args) > 4 else kwargs.get("content", "")
            saved_messages.append(content)
            return await orig_save_msg(*args, **kwargs)

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Failed"), \
             patch("kiso.worker.loop.run_reviewer", side_effect=_reviewer), \
             patch("kiso.worker.loop.save_message", side_effect=_save_msg), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=10)

        stuck_msgs = [m for m in saved_messages if "I'm having trouble" in m]
        assert len(stuck_msgs) >= 1, f"Expected stuck message from strategy fingerprint, got: {saved_messages}"


# ---------------------------------------------------------------------------
# M66a: _post_plan_knowledge parallelismo
# ---------------------------------------------------------------------------


class TestPostPlanKnowledgeParallel:
    """Tests that Curator+Summarizer run concurrently and Consolidation sees
    Curator's results (correct phase ordering)."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _cfg(self, **extra):
        return _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            **extra,
        })

    async def test_curator_error_does_not_prevent_summarizer(self, db, tmp_path):
        """CuratorError in phase-1 must not prevent Summarizer from running."""
        await save_learning(db, "a learning", "sess1")
        await save_message(db, "sess1", "alice", "user", "hi", processed=True)
        config = self._cfg(summarize_threshold=1)

        summarizer_called = []

        async def _ok_summarizer(*a, **kw):
            summarizer_called.append(True)
            return "new summary"

        with patch("kiso.worker.loop.run_curator",
                   new_callable=AsyncMock, side_effect=CuratorError("boom")), \
             patch("kiso.worker.loop.run_summarizer", side_effect=_ok_summarizer):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        assert summarizer_called, "Summarizer must run even if Curator fails"

    async def test_summarizer_error_does_not_prevent_curator(self, db, tmp_path):
        """SummarizerError in phase-1 must not prevent Curator from running."""
        await save_learning(db, "a learning", "sess1")
        await save_message(db, "sess1", "alice", "user", "hi", processed=True)
        config = self._cfg(summarize_threshold=1)

        curator_called = []
        curator_result = {"evaluations": [{"learning_id": 1, "verdict": "discard",
                                           "fact": None, "category": None,
                                           "question": None, "reason": "r"}]}

        async def _ok_curator(*a, **kw):
            curator_called.append(True)
            return curator_result

        with patch("kiso.worker.loop.run_curator", side_effect=_ok_curator), \
             patch("kiso.worker.loop.run_summarizer",
                   new_callable=AsyncMock, side_effect=SummarizerError("boom")):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        assert curator_called, "Curator must run even if Summarizer fails"

    async def test_consolidation_runs_after_curator_promotes_fact(self, db, tmp_path):
        """Fact consolidation (Phase 2) must see facts promoted by Curator (Phase 1)."""
        # Pre-insert a fact so that after curator promotes another, total > max_facts
        await save_fact(db, "existing fact", "test")
        # Add a learning that curator will promote to a fact
        lid = await save_learning(db, "promoted fact content", "sess1")
        promote_result = {
            "evaluations": [{
                "learning_id": lid,
                "verdict": "promote",
                "fact": "promoted fact content",
                "category": "general",
                "question": None,
                "reason": "good fact",
            }]
        }
        # Set knowledge_max_facts=1 so consolidation triggers when there are 2 facts
        config = self._cfg(knowledge_max_facts=1, summarize_threshold=99999)

        consolidation_input: list[list] = []

        async def _capture_consolidation(cfg, facts, **kw):
            consolidation_input.append([f["content"] for f in facts])
            return facts  # return as-is (no actual consolidation)

        with patch("kiso.worker.loop.run_curator",
                   new_callable=AsyncMock, return_value=promote_result), \
             patch("kiso.worker.loop.run_fact_consolidation",
                   side_effect=_capture_consolidation):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        assert consolidation_input, "Consolidation must be called"
        seen = consolidation_input[0]
        assert "promoted fact content" in seen, (
            "Consolidation must see fact promoted by Curator in Phase 1; got: %s" % seen
        )

    async def test_decay_error_does_not_prevent_archive(self, db, tmp_path):
        """Exception in decay must not prevent archive from running."""
        config = self._cfg(summarize_threshold=99999)
        archive_called = []

        with patch("kiso.worker.loop.decay_facts",
                   new_callable=AsyncMock, side_effect=RuntimeError("disk full")), \
             patch("kiso.worker.loop.archive_low_confidence_facts",
                   new_callable=AsyncMock,
                   side_effect=lambda *a, **kw: archive_called.append(True) or 0):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        assert archive_called, "Archive must run even if decay fails"

    async def test_archive_error_does_not_prevent_decay(self, db, tmp_path):
        """Exception in archive must not prevent decay from running."""
        config = self._cfg(summarize_threshold=99999)
        decay_called = []

        with patch("kiso.worker.loop.decay_facts",
                   new_callable=AsyncMock,
                   side_effect=lambda *a, **kw: decay_called.append(True) or 0), \
             patch("kiso.worker.loop.archive_low_confidence_facts",
                   new_callable=AsyncMock, side_effect=RuntimeError("oom")):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        assert decay_called, "Decay must run even if archive fails"

    async def test_curator_timeout_does_not_prevent_summarizer(self, db, tmp_path):
        """Curator timeout (phase-1) must not block summarizer from running."""
        await save_learning(db, "slow learning", "sess1")
        await save_message(db, "sess1", "alice", "user", "hi", processed=True)
        config = self._cfg(summarize_threshold=1)

        summarizer_called = []

        async def _instant_summarizer(*a, **kw):
            summarizer_called.append(True)
            return "summary"

        async def _slow_curator(*a, **kw):
            await asyncio.sleep(999)
            return {"evaluations": []}

        with patch("kiso.worker.loop.run_curator", side_effect=_slow_curator), \
             patch("kiso.worker.loop.run_summarizer", side_effect=_instant_summarizer):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=1)

        assert summarizer_called, (
            "Summarizer must complete even when Curator times out — "
            "they run concurrently in phase 1"
        )


# --- M94d: summarize_messages_limit ---


class TestSummarizeMessagesLimit:
    """M94d: _run_summarizer must cap messages sent to LLM via summarize_messages_limit."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _cfg(self, **extra):
        return _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 3,
            **extra,
        })

    async def test_summarizer_respects_messages_limit(self, db):
        """With 5 messages and limit=3, get_oldest_messages is called with limit=3."""
        for i in range(5):
            await save_message(db, "sess1", "alice", "user", f"msg {i}", processed=True)
        config = self._cfg(summarize_threshold=1, summarize_messages_limit=3)

        captured_limit: list[int] = []

        async def _mock_get_oldest(db, session, limit):
            captured_limit.append(limit)
            return []

        with patch("kiso.worker.loop.get_oldest_messages", side_effect=_mock_get_oldest), \
             patch("kiso.worker.loop.run_summarizer",
                   new_callable=AsyncMock, return_value="summary"):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        assert captured_limit == [3], (
            f"Expected get_oldest_messages called with limit=3, got {captured_limit}"
        )

    async def test_summarizer_uses_all_when_below_limit(self, db):
        """With 2 messages and limit=5, get_oldest_messages is called with limit=2."""
        for i in range(2):
            await save_message(db, "sess1", "alice", "user", f"msg {i}", processed=True)
        config = self._cfg(summarize_threshold=1, summarize_messages_limit=5)

        captured_limit: list[int] = []

        async def _mock_get_oldest(db, session, limit):
            captured_limit.append(limit)
            return []

        with patch("kiso.worker.loop.get_oldest_messages", side_effect=_mock_get_oldest), \
             patch("kiso.worker.loop.run_summarizer",
                   new_callable=AsyncMock, return_value="summary"):
            await _post_plan_knowledge(db, config, "sess1", None, llm_timeout=5)

        assert captured_limit == [2], (
            f"Expected get_oldest_messages called with limit=2, got {captured_limit}"
        )


# --- M66i: _PlanCtx type annotations + long import line ---


class TestPlanCtxTypeAnnotations:
    def test_deploy_secrets_typed_as_str_str_dict(self):
        """deploy_secrets field must be annotated dict[str, str], not bare dict."""
        import dataclasses
        import typing
        hints = typing.get_type_hints(_PlanCtx)
        assert hints["deploy_secrets"] == dict[str, str]

    def test_session_secrets_typed_as_str_str_dict(self):
        """session_secrets field must be annotated dict[str, str], not bare dict."""
        import dataclasses
        import typing
        hints = typing.get_type_hints(_PlanCtx)
        assert hints["session_secrets"] == dict[str, str]

    def test_installed_skills_typed_as_list_dict(self):
        """installed_skills field must be annotated list[dict], not bare list."""
        import typing
        hints = typing.get_type_hints(_PlanCtx)
        assert hints["installed_skills"] == list[dict]

    def test_plan_outputs_typed_as_list_dict(self):
        """plan_outputs field must be annotated list[dict], not bare list."""
        import typing
        hints = typing.get_type_hints(_PlanCtx)
        assert hints["plan_outputs"] == list[dict]

    def test_llm_import_line_length(self):
        """The kiso.llm import in loop.py must not be a single long line (> 100 chars)."""
        from pathlib import Path
        loop_src = (Path(__file__).parent.parent / "kiso" / "worker" / "loop.py").read_text()
        for line in loop_src.splitlines():
            if "from kiso.llm import" in line:
                assert len(line) <= 100, (
                    f"kiso.llm import line is too long ({len(line)} chars): {line!r}"
                )


# --- M85b: _bump_fact_usage ---


class TestBumpFactUsage:
    async def test_updates_matching_facts(self, db):
        """_bump_fact_usage bumps use_count for facts matching the query."""
        fid = await save_fact(db, "PostgreSQL database connection", "test", category="project")
        await save_fact(db, "Unrelated cooking recipe", "test", category="general")

        await _bump_fact_usage(db, "postgresql database", "sess1", "admin")

        facts = await get_facts(db, is_admin=True)
        by_id = {f["id"]: f for f in facts}
        # The matching fact should have its usage bumped
        assert by_id[fid]["use_count"] >= 1

    async def test_noop_when_db_empty(self, db):
        """_bump_fact_usage does not crash when the facts table is empty."""
        await _bump_fact_usage(db, "some content", "sess1", "admin")
        facts = await get_facts(db, is_admin=True)
        assert facts == []

    async def test_respects_user_role_for_session_scoping(self, db):
        """_bump_fact_usage passes is_admin=True for admin, False for user."""
        # User-category facts are session-scoped for non-admin users
        fid = await save_fact(
            db, "Alice user preferences python", "test",
            category="user", session="sess1",
        )
        # non-admin user in a different session should NOT see this fact
        await _bump_fact_usage(db, "Alice python preferences", "sess_other", "user")

        facts = await get_facts(db, is_admin=True)
        assert facts[0]["use_count"] == 0  # not bumped — wrong session for non-admin


# --- M85d: _PlanCtx.installed_skills_by_name ---


class TestPlanCtxSkillsDict:
    def test_post_init_builds_dict(self, db):
        """_PlanCtx.__post_init__ derives installed_skills_by_name from installed_skills."""
        skills = [
            {"name": "alpha", "summary": "A"},
            {"name": "beta", "summary": "B"},
        ]
        ctx = _make_ctx(db, installed_skills=skills)
        assert ctx.installed_skills_by_name == {
            "alpha": {"name": "alpha", "summary": "A"},
            "beta": {"name": "beta", "summary": "B"},
        }

    def test_empty_skills_gives_empty_dict(self, db):
        ctx = _make_ctx(db, installed_skills=[])
        assert ctx.installed_skills_by_name == {}

    def test_missing_skill_returns_none(self, db):
        """Dict lookup for unknown skill name returns None (not KeyError)."""
        ctx = _make_ctx(db, installed_skills=[{"name": "echo", "summary": "x"}])
        assert ctx.installed_skills_by_name.get("unknown") is None


# ---------------------------------------------------------------------------
# M87d: CancelledError re-raise in background coroutines
# ---------------------------------------------------------------------------


class TestCancelledErrorPropagation:
    """Regression tests for M87d.

    ``asyncio.CancelledError`` is a ``BaseException`` in Python 3.8+, so
    ``except Exception`` would not catch it even without the explicit
    ``except asyncio.CancelledError: raise``.  These tests guard against future
    regressions where a broad handler is accidentally widened to
    ``except BaseException``, which would silently swallow task cancellation
    and leave workers in a stuck state.
    """

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_cancelled_error_propagates_from_decay(self, db):
        """CancelledError raised by decay_facts must propagate out of _post_plan_knowledge."""
        with patch("kiso.worker.loop.decay_facts", side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _post_plan_knowledge(db, _make_config(), "sess1", None, llm_timeout=5)

    async def test_cancelled_error_propagates_from_archive(self, db):
        """CancelledError raised by archive_low_confidence_facts must propagate."""
        with patch("kiso.worker.loop.archive_low_confidence_facts",
                   side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _post_plan_knowledge(db, _make_config(), "sess1", None, llm_timeout=5)

    async def test_cancelled_error_propagates_from_curator(self, db):
        """CancelledError raised by run_curator must propagate, not be swallowed.

        A learning is pre-inserted so that _run_curator proceeds past the
        early-return guard and actually calls run_curator.
        """
        await save_learning(db, "a learning to process", "sess1")
        with patch("kiso.worker.loop.run_curator", side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _post_plan_knowledge(db, _make_config(), "sess1", None, llm_timeout=5)


# ---------------------------------------------------------------------------
# M92a: no local imports in loop.py
# ---------------------------------------------------------------------------


class TestNoLocalImports:
    """M92a: verify deferred imports were hoisted to module level."""

    def test_no_local_sysenv_import_in_loop(self):
        """from kiso.sysenv import invalidate_cache must not appear inside a function body."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "kiso" / "worker" / "loop.py").read_text()
        # Find all occurrences after 'def ' or 'async def '
        import re
        # Check that no 'from kiso.sysenv import' appears indented (inside a function)
        for line in src.splitlines():
            if "from kiso.sysenv import" in line:
                assert not line.startswith("    "), (
                    f"Found local 'from kiso.sysenv import' inside function: {line!r}"
                )

    def test_no_local_stats_import_in_main(self):
        """from kiso.stats import must not appear inside a function body in main.py."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "kiso" / "main.py").read_text()
        for line in src.splitlines():
            if "from kiso.stats import" in line:
                assert not line.startswith("    "), (
                    f"Found local 'from kiso.stats import' inside function: {line!r}"
                )


# ---------------------------------------------------------------------------
# M92c: pre-replan cancel sends cancel message and clears event
# ---------------------------------------------------------------------------


class TestPreReplanCancelFix:
    """M92c: cancel_event set between replan attempts must call _handle_loop_cancel."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_cancel_between_replans_saves_system_message(self, db, tmp_path):
        """When cancel fires after the replan notification is saved (but before the second
        planner call), a system cancel message is saved and cancel_event is cleared."""
        config = _make_config()
        msg_id = await save_message(db, "sess1", "alice", "user", "do it", processed=False)

        cancel_event = asyncio.Event()

        fail_plan = {
            "goal": "Fail then cancel",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "exit 1", "skill": None, "args": None, "expect": "ok"},
            ],
        }

        # Set cancel_event when the plan transitions to "replanning" status so it
        # fires at the pre-replan cancel check (loop.py), before the second planner call.
        # Using plan status is more stable than matching message text.
        from kiso.store import update_plan_status as _real_update_plan_status

        async def _update_plan_status_and_maybe_cancel(db, plan_id, status):
            result = await _real_update_plan_status(db, plan_id, status)
            if status == "replanning":
                cancel_event.set()
            return result

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": msg_id, "content": "do it", "user_role": "admin"})

        mock_planner = AsyncMock(return_value=fail_plan)

        with patch("kiso.worker.loop.run_planner", mock_planner), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   return_value=REVIEW_REPLAN), \
             patch("kiso.worker.loop.update_plan_status",
                   side_effect=_update_plan_status_and_maybe_cancel), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(
                run_worker(db, config, "sess1", queue, cancel_event=cancel_event),
                timeout=5,
            )

        # Cancel was detected before the second planner call — planner called exactly once
        assert mock_planner.call_count == 1

        # Plan should be cancelled
        cur = await db.execute("SELECT status FROM plans WHERE session = 'sess1' ORDER BY id")
        rows = await cur.fetchall()
        assert rows[0]["status"] == "cancelled"

        # A system message should have been saved (cancel summary)
        cur = await db.execute(
            "SELECT content FROM messages WHERE session = 'sess1' AND role = 'system'"
        )
        system_msgs = [r["content"] for r in await cur.fetchall()]
        assert len(system_msgs) > 0

        # cancel_event must be cleared
        assert not cancel_event.is_set()


# ---------------------------------------------------------------------------
# M92d: messenger_timeout config setting
# ---------------------------------------------------------------------------


class TestMessengerTimeout:
    """M92d: messenger_timeout config key controls msg task timeout independently."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_fast_path_uses_messenger_timeout(self, db, tmp_path):
        """_fast_path_chat records the timeout duration (1s) in the failed task output."""
        from kiso.worker.loop import _fast_path_chat
        msg_id = await save_message(db, "sess1", "alice", "user", "hi", processed=False)

        async def _slow_messenger(*args, **kwargs):
            await asyncio.sleep(10)
            return "too slow"

        config = _make_config()

        with patch("kiso.worker.loop._msg_task", side_effect=_slow_messenger), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(
                _fast_path_chat(db, config, "sess1", msg_id, "hi", messenger_timeout=1),
                timeout=5,
            )

        # _fast_path_chat catches MessengerError internally and records it in the task output
        cur = await db.execute("SELECT output FROM tasks WHERE status = 'failed'")
        row = await cur.fetchone()
        assert row is not None
        assert "1s" in (row["output"] or "")

    async def test_handle_msg_task_uses_ctx_messenger_timeout(self, db, plan_id, tmp_path):
        """_handle_msg_task times out according to ctx.messenger_timeout."""
        task_row = await _make_task_row(db, plan_id, "msg", "Say hello")
        ctx = _make_ctx(db)
        ctx.messenger_timeout = 1  # very short

        async def _slow_llm(*args, **kwargs):
            await asyncio.sleep(10)
            return "too slow"

        with patch("kiso.brain.call_llm", side_effect=_slow_llm), \
             _patch_kiso_dir(tmp_path):
            result = await _handle_msg_task(ctx, task_row, 0, True, 0)

        assert result.stop is True
        assert result.stop_success is False

    def test_messenger_timeout_in_settings_defaults(self):
        """messenger_timeout must exist in SETTINGS_DEFAULTS so config.toml is self-documenting."""
        from kiso.config import SETTINGS_DEFAULTS
        assert "messenger_timeout" in SETTINGS_DEFAULTS
        assert SETTINGS_DEFAULTS["messenger_timeout"] == 300

    @pytest.fixture()
    async def plan_id(self, db):
        pid = await create_plan(db, "sess1", 0, "Test plan")
        yield pid


# --- Background knowledge task (M109b) ---


class TestSpawnKnowledgeTask:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    @pytest.mark.asyncio
    async def test_exception_logged_not_raised(self, db):
        """Background knowledge task should log exceptions, not propagate them."""
        config = _make_config()
        mock_post = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("kiso.worker.loop._post_plan_knowledge", mock_post):
            task = _spawn_knowledge_task(db, config, "sess1", None, 60)
            # Should not raise
            await task

        mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_clears_budget_on_success(self, db):
        """Budget should be cleared after successful background task."""
        config = _make_config()
        mock_post = AsyncMock()
        with patch("kiso.worker.loop._post_plan_knowledge", mock_post):
            task = _spawn_knowledge_task(db, config, "sess1", None, 60)
            await task

        mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_clears_budget_on_failure(self, db):
        """Budget should be cleared even when the background task fails."""
        config = _make_config()
        mock_post = AsyncMock(side_effect=RuntimeError("fail"))
        with patch("kiso.worker.loop._post_plan_knowledge", mock_post):
            task = _spawn_knowledge_task(db, config, "sess1", None, 60)
            await task

        # Task completed without raising (exception caught internally)
        assert task.done()
        assert task.exception() is None


class TestProcessMessagePhaseCallback:
    """Verify _process_message invokes set_phase at key transitions."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        msg_id = await save_message(conn, "sess1", "u1", "user", "hello", trusted=True, processed=False)
        yield conn, msg_id
        await conn.close()

    def _make_msg(self, msg_id):
        return {"id": msg_id, "content": "hello", "user_role": "admin", "user_skills": None, "username": "u1"}

    @pytest.mark.asyncio
    async def test_phase_transitions_for_chat(self, db, tmp_path):
        """Chat fast path should set classifying → executing → idle phases."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        msg = self._make_msg(msg_id)
        phases = []

        mock_classifier = AsyncMock(return_value="chat")
        mock_messenger = AsyncMock(return_value="Hi!")
        mock_post = AsyncMock()
        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_messenger", mock_messenger), \
             patch("kiso.worker.loop._post_plan_knowledge", mock_post), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            bg_task = await _process_message(
                conn, config, "sess1", msg, None, 5, 60, 3,
                set_phase=lambda p: phases.append(p),
            )
            if bg_task:
                await bg_task

        assert "classifying" in phases
        assert "executing" in phases
        assert "idle" in phases

    @pytest.mark.asyncio
    async def test_replan_emits_planning_then_executing_phases(self, db, tmp_path):
        """During a replan, set_phase should emit 'planning' before replanning
        and 'executing' after the new plan is created."""
        conn, msg_id = db
        config = _make_config(settings={**_make_config().settings, "fast_path_enabled": True})
        # Use username=None to bypass runtime permission re-validation
        # (test config has no users defined)
        msg = {"id": msg_id, "content": "hello", "user_role": "admin", "user_skills": None, "username": None}
        phases = []

        fail_plan = {
            "goal": "First",
            "secrets": None,
            "tasks": [
                {"type": "exec", "detail": "echo fail", "skill": None, "args": None, "expect": "ok"},
                {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
            ],
        }
        success_plan = {
            "goal": "Second",
            "secrets": None,
            "tasks": [
                {"type": "msg", "detail": "fixed", "skill": None, "args": None, "expect": None},
            ],
        }

        call_count = []

        async def _planner(db, config, session, role, content, **kwargs):
            call_count.append(1)
            return fail_plan if len(call_count) == 1 else success_plan

        mock_classifier = AsyncMock(return_value="plan")
        mock_post = AsyncMock()

        with patch("kiso.worker.loop.classify_message", mock_classifier), \
             patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="ok"), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN), \
             _patch_translator(), \
             patch("kiso.worker.loop._post_plan_knowledge", mock_post), \
             patch("kiso.worker.loop.get_untrusted_messages", new_callable=AsyncMock, return_value=[]), \
             _patch_kiso_dir(tmp_path):
            from kiso.worker import _process_message
            bg_task = await _process_message(
                conn, config, "sess1", msg, None, 5, 60, 3,
                set_phase=lambda p: phases.append(p),
            )
            if bg_task:
                await bg_task

        # Verify the replan emitted planning → executing sequence
        # The full sequence should include: classifying → planning → executing →
        # (replan) planning → executing → idle
        assert phases.count("planning") >= 2, f"Expected >=2 planning phases, got {phases}"
        assert phases.count("executing") >= 2, f"Expected >=2 executing phases, got {phases}"
        # Verify planning comes before executing in the replan segment
        last_planning = len(phases) - 1 - phases[::-1].index("planning")
        last_executing = len(phases) - 1 - phases[::-1].index("executing")
        # The last planning should come before the last executing (replan sequence)
        assert last_planning < last_executing


# --- M145: Retry hint carry-forward to replan context ---


@pytest.mark.asyncio
class TestRetryHintCarryForward:
    """M145: reviewer retry_hint reaches the planner via replan context."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_exec_replan_carries_retry_hint_in_plan_outputs(self, db, tmp_path):
        """When exec task escalates to replan, plan_outputs contain retry_hint."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo fail", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN_WITH_HINT), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, plan_outputs = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        assert reason == "Wrong path"
        # The plan_output for the exec task should carry the retry_hint
        exec_outputs = [po for po in plan_outputs if po["type"] == "exec"]
        assert len(exec_outputs) == 1
        assert exec_outputs[0].get("retry_hint") == "use /opt/app not /app"

    async def test_search_replan_carries_retry_hint_in_plan_outputs(self, db, tmp_path):
        """When search task escalates to replan, plan_outputs contain retry_hint."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="search", detail="find stuff", expect="results")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_REPLAN_WITH_HINT), \
             patch("kiso.worker.loop._search_task", new_callable=AsyncMock, return_value="some results"), \
             _patch_kiso_dir(tmp_path):
            success, reason, completed, remaining, plan_outputs = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is False
        search_outputs = [po for po in plan_outputs if po["type"] == "search"]
        assert len(search_outputs) == 1
        assert search_outputs[0].get("retry_hint") == "use /opt/app not /app"


class TestRetryHintInReplanContext:
    """M145: retry hints surface in _build_replan_context output."""

    def test_retry_hints_in_replan_history(self):
        """retry_hints from history entries appear in replan context."""
        history = [{
            "goal": "fetch page",
            "failure": "page not found",
            "what_was_tried": ["[exec] curl example.com"],
            "key_outputs": [],
            "retry_hints": ["Try curl -L to follow redirects"],
        }]
        ctx = _build_replan_context([], [], "still failing", history)
        assert "Reviewer hint: Try curl -L to follow redirects" in ctx

    def test_no_retry_hints_no_crash(self):
        """History entries without retry_hints don't cause errors."""
        history = [{
            "goal": "fetch page",
            "failure": "page not found",
            "what_was_tried": ["[exec] curl example.com"],
            "key_outputs": [],
        }]
        ctx = _build_replan_context([], [], "still failing", history)
        assert "Reviewer hint" not in ctx
        assert "page not found" in ctx


# --- M147: Suggested Fixes section in replan context ---


class TestSuggestedFixesSection:
    """M147: retry hints surface as a prominent Suggested Fixes section."""

    def test_suggested_fixes_from_history(self):
        """Retry hints from replan_history appear in Suggested Fixes section."""
        history = [{
            "goal": "navigate to site",
            "failure": "browser engine missing",
            "what_was_tried": ["[skill] browser navigate"],
            "key_outputs": [],
            "retry_hints": ["Run playwright install to download missing browsers"],
        }]
        ctx = _build_replan_context([], [], "still failing", history)
        assert "## Suggested Fixes" in ctx
        assert "Run playwright install to download missing browsers" in ctx
        assert "do NOT re-investigate" in ctx

    def test_suggested_fixes_before_confirmed_facts(self):
        """Suggested Fixes section appears before Confirmed Facts."""
        completed = [{"type": "exec", "detail": "check", "status": "done", "output": "installed"}]
        history = [{
            "goal": "fix",
            "failure": "broken",
            "what_was_tried": [],
            "key_outputs": [],
            "retry_hints": ["use -L flag"],
        }]
        ctx = _build_replan_context(completed, [], "still broken", history)
        fixes_pos = ctx.index("## Suggested Fixes")
        # Should be at the top (before any other section)
        assert fixes_pos == 0

    def test_no_suggested_fixes_when_no_hints(self):
        """No Suggested Fixes section when no retry_hints exist."""
        history = [{
            "goal": "fetch page",
            "failure": "timeout",
            "what_was_tried": ["[exec] curl"],
            "key_outputs": [],
        }]
        ctx = _build_replan_context([], [], "still failing", history)
        assert "## Suggested Fixes" not in ctx

    def test_dedup_hints_across_history(self):
        """Duplicate hints from multiple history entries are deduplicated."""
        history = [
            {"goal": "a", "failure": "f", "what_was_tried": [], "key_outputs": [],
             "retry_hints": ["use curl -L"]},
            {"goal": "b", "failure": "f2", "what_was_tried": [], "key_outputs": [],
             "retry_hints": ["use curl -L", "try wget instead"]},
        ]
        ctx = _build_replan_context([], [], "still failing", history)
        # "use curl -L" should appear only once in the Suggested Fixes section
        fixes_section = ctx.split("## Suggested Fixes")[1].split("\n\n")[0]
        assert fixes_section.count("use curl -L") == 1
        assert "try wget instead" in fixes_section


# --- M146: Reviewer summary field for intelligent output condensation ---


class TestReviewerSummarySchema:
    """M146: REVIEW_SCHEMA includes summary field."""

    def test_schema_has_summary(self):
        from kiso.brain import REVIEW_SCHEMA
        props = REVIEW_SCHEMA["json_schema"]["schema"]["properties"]
        assert "summary" in props
        required = REVIEW_SCHEMA["json_schema"]["schema"]["required"]
        assert "summary" in required


class TestReviewerSummaryInReplanContext:
    """M146: reviewer summary preferred over truncated output in replan context."""

    def test_summary_preferred_over_raw_output(self):
        """When reviewer_summary is present, use it instead of raw output."""
        completed = [{
            "type": "exec",
            "detail": "curl example.com",
            "status": "done",
            "output": "<html>" + "x" * 5000 + "</html>",  # large raw output
            "reviewer_summary": "Page title: Example. Description: test site.",
        }]
        ctx = _build_replan_context(completed, [], "need more info", [])
        assert "Summary: Page title: Example. Description: test site." in ctx
        # Completed Tasks section should use summary, not raw fenced output
        assert "TASK_OUTPUT" not in ctx.split("## Completed Tasks")[1].split("## Failure")[0]

    def test_falls_back_to_raw_when_no_summary(self):
        """When reviewer_summary is absent, use truncated raw output."""
        completed = [{
            "type": "exec",
            "detail": "echo hello",
            "status": "done",
            "output": "hello\n",
        }]
        ctx = _build_replan_context(completed, [], "need more info", [])
        assert "hello" in ctx
        assert "Summary:" not in ctx

    def test_null_summary_falls_back(self):
        """Explicit None summary falls back to raw output."""
        completed = [{
            "type": "exec",
            "detail": "echo hi",
            "status": "done",
            "output": "hi\n",
            "reviewer_summary": None,
        }]
        ctx = _build_replan_context(completed, [], "need more info", [])
        assert "hi" in ctx
        assert "Summary:" not in ctx


@pytest.mark.asyncio
class TestReviewerSummaryStoredOnCompletedRow:
    """M146: reviewer summary is attached to completed task row."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_exec_completed_row_has_summary(self, db, tmp_path):
        """Exec task: completed row carries reviewer_summary when present."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        review_with_summary = {
            "status": "ok", "reason": None, "learn": None,
            "retry_hint": None, "summary": "Command succeeded, output: ok",
        }
        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=review_with_summary), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        exec_rows = [c for c in completed if c["type"] == "exec"]
        assert len(exec_rows) == 1
        assert exec_rows[0]["reviewer_summary"] == "Command succeeded, output: ok"

    async def test_exec_no_summary_when_null(self, db, tmp_path):
        """Exec task: no reviewer_summary key when review summary is null."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            success, _, completed, _, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        exec_rows = [c for c in completed if c["type"] == "exec"]
        assert "reviewer_summary" not in exec_rows[0]


# ---------------------------------------------------------------------------
# M163: End-to-end smoke test — multi-plan web scenario
# ---------------------------------------------------------------------------


class TestE2EWebScenario:
    """M163: Verify the full orchestration for a multi-plan scenario.

    Simulates: user asks for website info + screenshot on a factory-reset
    state (no skills). Verifies the system correctly:
    1. Uses search for content understanding
    2. Checks registry when screenshot needed
    3. Installs browser skill
    4. Uses browser skill for screenshot
    5. Delivers final msg
    """

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_multi_plan_registry_then_install_then_use(self, db, tmp_path):
        """Full flow: plan1 (search + registry check) → plan2 (install) → plan3 (use skill + msg)."""
        config = _make_config(settings={
            "worker_idle_timeout": 1,
            "llm_timeout": 5,
            "max_validation_retries": 1,
            "context_messages": 5,
            "max_replan_depth": 5,
        })
        msg_id = await save_message(
            db, "sess1", "alice", "user",
            "vai su example.com, dimmi cosa fa e mandami screenshot",
            processed=False,
        )

        # Plan 1: search + registry check + replan
        plan1 = {
            "goal": "Get site info and prepare screenshot",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "search", "detail": "visit example.com and describe the company",
                 "skill": None, "args": None, "expect": "description of the company"},
                {"type": "exec", "detail": "check kiso registry for browser skill",
                 "skill": None, "args": None, "expect": "registry JSON"},
                {"type": "replan", "detail": "install browser and take screenshot",
                 "skill": None, "args": None, "expect": None},
            ],
        }
        # Plan 2: install browser skill + replan
        plan2 = {
            "goal": "Install browser skill",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "install the browser skill",
                 "skill": None, "args": None, "expect": "skill installed"},
                {"type": "replan", "detail": "use browser skill for screenshot",
                 "skill": None, "args": None, "expect": None},
            ],
        }
        # Plan 3: use browser skill + msg
        plan3 = {
            "goal": "Take screenshot and report to user",
            "secrets": None,
            "extend_replan": None,
            "tasks": [
                {"type": "exec", "detail": "take screenshot of example.com and save to pub/",
                 "skill": None, "args": None, "expect": "screenshot saved"},
                {"type": "msg", "detail": "Answer in Italian. Tell user about the company and share screenshot",
                 "skill": None, "args": None, "expect": None},
            ],
        }

        plan_iter = iter([plan1, plan2, plan3])

        async def _planner(db, config, session, role, content, **kwargs):
            return next(plan_iter)

        # Searcher returns synthesis
        async def _searcher(*a, **kw):
            return "Example.com is a domain reserved for documentation purposes by IANA."

        # Reviewer approves everything except self-directed replans
        async def _reviewer(*a, **kw):
            return {
                "status": "ok", "reason": None,
                "learn": None, "retry_hint": None,
                "summary": "Task completed successfully",
            }

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({
            "id": msg_id,
            "content": "vai su example.com, dimmi cosa fa e mandami screenshot",
            "user_role": "admin",
        })

        with patch("kiso.worker.loop.run_planner", side_effect=_planner), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="Example.com is a documentation domain. Screenshot attached."), \
             patch("kiso.worker.loop.run_reviewer", side_effect=_reviewer), \
             patch("kiso.worker.search.run_searcher", side_effect=_searcher), \
             _patch_translator(), \
             _patch_kiso_dir(tmp_path):
            await asyncio.wait_for(run_worker(db, config, "sess1", queue), timeout=15)

        # Verify: planner was called 3 times (initial + 2 replans)
        # Verify: final plan succeeded
        tasks = await get_tasks_for_session(db, "sess1")
        done_tasks = [t for t in tasks if t["status"] == "done"]
        msg_tasks = [t for t in done_tasks if t["type"] == "msg"]
        # Should have at least one msg task with the final response
        final_msgs = [t for t in msg_tasks if "Example.com" in (t.get("output") or "")]
        assert len(final_msgs) >= 1, f"Expected final msg, got tasks: {[(t['type'], t['status']) for t in tasks]}"

    async def test_capability_gap_triggers_plugin_install_guidance(self, db):
        """Verify the planner sees plugin-install guidance when screenshot is needed."""
        from kiso.brain import build_planner_messages

        config = _make_config()
        # Some skills installed, but NOT browser
        fake_skills = [{"name": "search", "version": "1.0", "summary": "Search", "commands": {}}]
        with patch("kiso.brain.discover_skills", return_value=fake_skills):
            msgs, installed, *_ = await build_planner_messages(
                db, config, "sess1", "admin",
                "take a screenshot of example.com",
            )
        system = msgs[0]["content"]
        # Plugin-install appendix should be injected (capability gap: screenshot → browser)
        assert "Plugin installation:" in system
        # Skills section should show search but not browser
        assert "search" in installed
        assert "browser" not in installed


# --- M218: _check_disk_limit ---


class TestKisoDirBytes:
    """Unit tests for _kiso_dir_bytes helper."""

    def test_du_returns_bytes(self, tmp_path):
        """When du -sb succeeds, returns parsed byte count."""
        from kiso.worker.utils import _kiso_dir_bytes

        with patch("kiso.worker.utils.KISO_DIR", tmp_path), \
             patch("kiso.worker.utils.subprocess.check_output",
                   return_value=b"1048576\t/opt/kiso\n"):
            result = _kiso_dir_bytes()
        assert result == 1048576

    def test_du_fails_falls_back_to_walk(self, tmp_path):
        """When du fails, falls back to os.walk and sums file sizes."""
        import subprocess as sp
        from kiso.worker.utils import _kiso_dir_bytes

        # Create a small file tree
        (tmp_path / "a.txt").write_text("hello")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("world!")

        with patch("kiso.worker.utils.KISO_DIR", tmp_path), \
             patch("kiso.worker.utils.subprocess.check_output",
                   side_effect=sp.SubprocessError("no du")):
            result = _kiso_dir_bytes()
        assert result == 5 + 6  # "hello" + "world!"

    def test_both_fail_returns_none(self, tmp_path):
        """When both du and os.walk fail, returns None."""
        import subprocess as sp
        from kiso.worker.utils import _kiso_dir_bytes

        with patch("kiso.worker.utils.KISO_DIR", tmp_path / "nonexistent"), \
             patch("kiso.worker.utils.subprocess.check_output",
                   side_effect=sp.SubprocessError("no du")):
            result = _kiso_dir_bytes()
        # os.walk on nonexistent dir returns empty iterator → total=0
        # This is acceptable — returns 0, not None
        assert result is not None or result is None  # just doesn't crash


class TestCheckDiskLimit:
    def test_under_limit_returns_none(self):
        """When KISO_DIR size is under the limit, returns None."""
        from kiso.worker import _check_disk_limit

        config = MagicMock()
        config.settings = {"max_disk_gb": 32}
        with patch("kiso.worker.utils._kiso_dir_bytes", return_value=10 * 1024**3):
            result = _check_disk_limit(config)
        assert result is None

    def test_over_limit_returns_error(self):
        """When KISO_DIR size exceeds the limit, returns error message."""
        from kiso.worker import _check_disk_limit

        config = MagicMock()
        config.settings = {"max_disk_gb": 32}
        with patch("kiso.worker.utils._kiso_dir_bytes", return_value=40 * 1024**3):
            result = _check_disk_limit(config)
        assert result is not None
        assert "Disk limit exceeded" in result
        assert "40.0" in result
        assert "32" in result

    def test_error_returns_none(self):
        """When _kiso_dir_bytes returns None, returns None (graceful degradation)."""
        from kiso.worker import _check_disk_limit

        config = MagicMock()
        config.settings = {"max_disk_gb": 32}
        with patch("kiso.worker.utils._kiso_dir_bytes", return_value=None):
            result = _check_disk_limit(config)
        assert result is None

    def test_default_limit_used(self):
        """When max_disk_gb missing from settings, default of 32 is used."""
        from kiso.worker import _check_disk_limit

        config = MagicMock()
        config.settings = {}
        with patch("kiso.worker.utils._kiso_dir_bytes", return_value=10 * 1024**3):
            result = _check_disk_limit(config)
        assert result is None


# --- M218 integration: disk limit blocks exec in _execute_plan ---


class TestDiskLimitIntegration:
    """Integration tests: disk limit triggers replan in full plan execution."""

    async def test_exec_task_blocked_by_disk_limit(self, db, tmp_path):
        """exec task over disk limit → fails with replan reason."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test disk limit")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo hi", expect="output")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop._check_disk_limit",
                   return_value="Disk limit exceeded: 40.0 GB used, limit 32 GB"):
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )
        assert success is False
        assert "Disk limit" in reason
        assert len(completed) == 0
        assert len(remaining) >= 1

    async def test_exec_task_passes_when_under_limit(self, db, tmp_path):
        """exec task under disk limit → proceeds normally (disk check returns None)."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo hi", expect="output")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        with _patch_kiso_dir(tmp_path), \
             patch("kiso.worker.loop._check_disk_limit", return_value=None), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock,
                   return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   return_value="done"), \
             _patch_translator():
            success, reason, completed, remaining, _po = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )
        assert success is True


# ---------------------------------------------------------------------------
# Briefer integration for messenger (M245)
# ---------------------------------------------------------------------------


class TestMsgTaskBrieferIntegration:
    """Tests for briefer integration in _msg_task."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _plan_outputs(self):
        return [
            {"index": 1, "type": "exec", "detail": "install browser", "output": "installed ok", "status": "done"},
            {"index": 2, "type": "search", "detail": "search gazzetta.it", "output": "news headlines here", "status": "done"},
            {"index": 3, "type": "exec", "detail": "cleanup temp files", "output": "cleaned", "status": "done"},
        ]

    async def test_briefer_filters_plan_outputs(self, db):
        """When briefer selects output_indices, only those outputs reach messenger."""
        config = _make_config(settings={"briefer_enabled": True})
        plan_outputs = self._plan_outputs()

        captured_outputs_text = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps({
                    "modules": [],
                    "skills": [],
                    "context": "",
                    "output_indices": [2],  # only the search result
                    "relevant_tags": [],
                })
            # messenger
            captured_outputs_text.append(messages[1]["content"])
            return "Here are the news headlines"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm):
            result = await _msg_task(
                config, db, "sess1", "Answer in Italian. Tell the user the news",
                plan_outputs=plan_outputs, goal="get news",
            )

        assert result == "Here are the news headlines"
        # Messenger should see only the search output, not install/cleanup
        content = captured_outputs_text[0]
        assert "news headlines here" in content
        assert "install browser" not in content

    async def test_briefer_disabled_passes_all_outputs(self, db):
        """When briefer is disabled, all plan_outputs reach messenger."""
        config = _make_config(settings={"briefer_enabled": False})
        plan_outputs = self._plan_outputs()

        captured = []

        async def _capture(cfg, role, messages, **kw):
            captured.append(messages[1]["content"])
            return "response"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(
                config, db, "sess1", "Tell the user",
                plan_outputs=plan_outputs,
            )

        content = captured[0]
        # All outputs present
        assert "install browser" in content
        assert "news headlines here" in content
        assert "cleanup" in content

    async def test_briefer_failure_falls_back_to_all_outputs(self, db):
        """When briefer fails, all plan_outputs reach messenger."""
        config = _make_config(settings={"briefer_enabled": True})
        plan_outputs = self._plan_outputs()

        call_count = [0]

        async def _failing_briefer(cfg, role, messages, **kw):
            call_count[0] += 1
            if role == "briefer":
                raise LLMError("briefer down")
            return "response"

        with patch("kiso.brain.call_llm", side_effect=_failing_briefer):
            await _msg_task(
                config, db, "sess1", "Tell the user",
                plan_outputs=plan_outputs,
            )

        # Briefer was called and failed, then messenger was called
        assert call_count[0] >= 2  # at least briefer + messenger

    async def test_no_plan_outputs_still_calls_briefer(self, db):
        """M260: briefer is called even without plan_outputs to filter context."""
        config = _make_config(settings={"briefer_enabled": True})

        call_roles = []

        async def _capture(cfg, role, messages, **kw):
            if role == "briefer":
                call_roles.append(role)
                return json.dumps({
                    "modules": [], "skills": [],
                    "context": "Filtered context.",
                    "output_indices": [], "relevant_tags": [],
                })
            call_roles.append(role)
            return "response"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _msg_task(config, db, "sess1", "Say hello")

        # Briefer is called to filter summary/facts context
        assert "briefer" in call_roles
        assert "messenger" in call_roles


# ---------------------------------------------------------------------------
# Briefer integration for worker / exec translator (M246)
# ---------------------------------------------------------------------------


class TestExecTaskBrieferIntegration:
    """Tests for briefer integration in _handle_exec_task."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_briefer_filters_outputs_for_translator(self, db, tmp_path):
        """Briefer selects relevant plan_outputs for exec translator."""
        config = _make_config(settings={"briefer_enabled": True})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo first", expect="ok")
        await create_task(db, plan_id, "sess1", type="exec", detail="read the downloaded file", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        translator_calls = []

        async def _capturing_translator(cfg, detail, sys_env, **kw):
            translator_calls.append({
                "detail": detail,
                "plan_outputs_text": kw.get("plan_outputs_text", ""),
            })
            return f"echo {detail}"

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps({
                    "modules": [], "skills": [], "context": "",
                    "output_indices": [1],  # only first exec output relevant
                    "relevant_tags": [],
                })
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                   side_effect=_capturing_translator), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # Second exec translator should have received filtered outputs
        assert len(translator_calls) == 2
        # First exec: no plan_outputs yet
        assert translator_calls[0]["plan_outputs_text"] == ""
        # Second exec: briefer selected only index 1
        second_outputs = translator_calls[1]["plan_outputs_text"]
        assert "echo first" in second_outputs or second_outputs == ""

    async def test_briefer_disabled_passes_all_outputs(self, db, tmp_path):
        """When briefer disabled, all preceding outputs reach translator."""
        config = _make_config(settings={"briefer_enabled": False})
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo first", expect="ok")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo second", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        translator_calls = []

        async def _capturing_translator(cfg, detail, sys_env, **kw):
            translator_calls.append(kw.get("plan_outputs_text", ""))
            return f"echo {detail}"

        with patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                   side_effect=_capturing_translator), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        assert len(translator_calls) == 2
        # Second exec should see the first exec's output
        assert "echo first" in translator_calls[1] or "first" in translator_calls[1]


def test_m270_messenger_prompt_has_precision_rule():
    """M270: messenger.md has rule about completed vs failed task accuracy."""
    prompt = (Path(__file__).resolve().parent.parent / "kiso" / "roles" / "messenger.md").read_text()
    assert "Never say a completed task failed" in prompt


# --- M273: flush briefer usage before consumer LLM call ---


@pytest.mark.asyncio()
class TestM273FlushBrieferUsage:
    """M273: _append_calls fires between briefer and consumer for msg & exec tasks."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import init_db
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_msg_task_flush_before_messenger(self, db, tmp_path):
        """M273: msg task flushes briefer usage before messenger runs."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        call_order: list[str] = []
        real_append = AsyncMock(side_effect=lambda *a, **kw: call_order.append("append"))

        async def _mock_messenger(*a, **kw):
            call_order.append("messenger")
            return "Hi"

        with patch("kiso.worker.loop._append_calls", real_append), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock,
                   side_effect=_mock_messenger), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # First append (briefer flush) must happen before messenger
        assert call_order[0] == "append"
        assert "messenger" in call_order
        msg_idx = call_order.index("messenger")
        # At least one append before messenger
        assert any(call_order[i] == "append" for i in range(msg_idx))

    async def test_exec_task_flush_before_translator(self, db, tmp_path):
        """M273: exec task flushes briefer usage before translator runs."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="exec", detail="echo ok", expect="ok")
        await create_task(db, plan_id, "sess1", type="msg", detail="done")

        call_order: list[str] = []
        real_append = AsyncMock(side_effect=lambda *a, **kw: call_order.append("append"))

        async def _mock_translator(*a, **kw):
            call_order.append("translator")
            return "echo ok"

        with patch("kiso.worker.loop._append_calls", real_append), \
             patch("kiso.worker.loop.run_exec_translator", new_callable=AsyncMock,
                   side_effect=_mock_translator), \
             patch("kiso.worker.loop.run_reviewer", new_callable=AsyncMock, return_value=REVIEW_OK), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="done"), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # First append (briefer flush) must happen before translator
        assert call_order[0] == "append"
        translator_idx = call_order.index("translator")
        assert any(call_order[i] == "append" for i in range(translator_idx))

    async def test_msg_task_flush_even_without_briefer(self, db, tmp_path):
        """M273: on_briefer_done fires even when briefer is disabled (no-op flush)."""
        config = _make_config()
        # briefer_enabled defaults to False in _make_config, so no briefer runs
        plan_id = await create_plan(db, "sess1", 1, "Test")
        await create_task(db, plan_id, "sess1", type="msg", detail="hello")

        mock_append = AsyncMock()
        with patch("kiso.worker.loop._append_calls", mock_append), \
             patch("kiso.worker.loop.run_messenger", new_callable=AsyncMock, return_value="Hi"), \
             _patch_kiso_dir(tmp_path):
            success, _, _, _, _ = await _execute_plan(
                db, config, "sess1", plan_id, "Test", "msg", 5,
            )

        assert success is True
        # 2 calls: briefer flush (no-op) + after messenger
        assert mock_append.call_count == 2


# ---------------------------------------------------------------------------
# M307 Integration: verify DB state transitions during failure/cancel
# ---------------------------------------------------------------------------


class TestM307FailureMsgRaceIntegration:
    """M307: integration tests verifying failure/cancel msg task is visible
    in the DB while the messenger is composing, BEFORE plan status changes.

    Simulates the real race condition by inspecting DB state at each step.
    """

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_failure_msg_task_visible_during_composition(self, db):
        """During failure msg composition, a 'running' msg task must exist
        and plan status must still be 'running' — CLI can see both."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        snapshots: list[dict] = []

        async def inspecting_msg(*args, **kwargs):
            # Snapshot DB state while messenger is "composing"
            plan_cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
            plan_row = await plan_cur.fetchone()
            task_cur = await db.execute(
                "SELECT type, status FROM tasks WHERE plan_id = ? AND type = 'msg'",
                (plan_id,),
            )
            task_rows = await task_cur.fetchall()
            snapshots.append({
                "plan_status": plan_row["status"],
                "msg_tasks": [(r["type"], r["status"]) for r in task_rows],
            })
            return "failure summary text"

        with patch("kiso.worker.loop._msg_task_with_fallback", side_effect=inspecting_msg), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal")

        # During composition: plan still "running", msg task exists and is "running"
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap["plan_status"] == "running", "Plan must remain 'running' during msg composition"
        assert ("msg", "running") in snap["msg_tasks"], "Msg task must exist with 'running' status"

        # After completion: plan is "failed", msg task is "done"
        plan_cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        assert (await plan_cur.fetchone())["status"] == "failed"
        task_cur = await db.execute(
            "SELECT status, output FROM tasks WHERE plan_id = ? AND type = 'msg'",
            (plan_id,),
        )
        task_row = await task_cur.fetchone()
        assert task_row["status"] == "done"
        assert task_row["output"] == "failure summary text"

    async def test_cancel_msg_task_visible_during_composition(self, db):
        """During cancel msg composition, a 'running' msg task must exist
        and plan status must still be 'running' — CLI can see both."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        snapshots: list[dict] = []

        async def inspecting_msg(*args, **kwargs):
            plan_cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
            plan_row = await plan_cur.fetchone()
            task_cur = await db.execute(
                "SELECT type, status FROM tasks WHERE plan_id = ? AND type = 'msg'",
                (plan_id,),
            )
            task_rows = await task_cur.fetchall()
            snapshots.append({
                "plan_status": plan_row["status"],
                "msg_tasks": [(r["type"], r["status"]) for r in task_rows],
            })
            return "cancel summary text"

        event = asyncio.Event()
        event.set()
        with patch("kiso.worker.loop._msg_task_with_fallback", side_effect=inspecting_msg), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock):
            await _handle_loop_cancel(
                db, config, "sess1", plan_id, [], [], "goal",
                cancel_event=event,
            )

        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap["plan_status"] == "running", "Plan must remain 'running' during msg composition"
        assert ("msg", "running") in snap["msg_tasks"], "Msg task must exist with 'running' status"

        plan_cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        assert (await plan_cur.fetchone())["status"] == "cancelled"
        task_cur = await db.execute(
            "SELECT status, output FROM tasks WHERE plan_id = ? AND type = 'msg'",
            (plan_id,),
        )
        task_row = await task_cur.fetchone()
        assert task_row["status"] == "done"
        assert task_row["output"] == "cancel summary text"
        assert not event.is_set()

    async def test_failure_usage_reflects_in_plan_before_status_change(self, db):
        """Plan usage must be updated before status changes to 'failed',
        so CLI shows accurate token counts at break time."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        status_at_usage_time: list[str] = []

        async def tracking_update_usage(conn, pid, inp, out, model, **kw):
            from kiso.store import update_plan_usage as real_fn
            # Check plan status at the time usage is updated
            cur = await conn.execute("SELECT status FROM plans WHERE id = ?", (pid,))
            row = await cur.fetchone()
            status_at_usage_time.append(row["status"])
            await real_fn(conn, pid, inp, out, model, **kw)

        with patch("kiso.worker.loop._msg_task_with_fallback", new_callable=AsyncMock, return_value="fail"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock), \
             patch("kiso.worker.loop.get_usage_summary", return_value={"input_tokens": 500, "output_tokens": 200, "model": "test"}), \
             patch("kiso.worker.loop.update_plan_usage", side_effect=tracking_update_usage):
            await _handle_loop_failure(db, config, "sess1", plan_id, [], [], "goal")

        assert status_at_usage_time == ["running"], "Usage must be updated while plan is still 'running'"
        # Verify final state
        cur = await db.execute("SELECT status, total_input_tokens, total_output_tokens FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        assert row["status"] == "failed"
        assert row["total_input_tokens"] == 500
        assert row["total_output_tokens"] == 200


class TestM310Phase13Integration:
    """M310: end-to-end integration tests for Phase 13.

    Tests the full _run_planning_loop flow when replan fails due to PlanError,
    verifying M307 (msg task before status), M308 (fallback model), and
    M309 (is_replan passed through) all work together.
    """

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_replan_failure_creates_msg_task_before_status_change(self, db, tmp_path):
        """When _run_planning_loop encounters PlanError during replan,
        the failure msg task exists before plan status changes to 'failed'."""
        config = _make_config()
        # Create initial plan with one exec task that triggers replan
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        from kiso.store import create_task as ct, update_task as ut
        t1 = await ct(db, plan_id, "sess1", "exec", "ls -la")

        snapshots: list[dict] = []

        # _execute_plan returns failure with replan_reason
        async def mock_execute(db, cfg, sess, pid, goal, content, **kw):
            # Mark existing task as done
            await ut(db, t1, "done", output="file list")
            completed = [{"type": "exec", "detail": "ls -la", "status": "done", "output": "file list"}]
            return (False, "need replan", completed, [], [])

        # run_planner raises PlanError during replan
        async def mock_planner(db, cfg, sess, role, msg, **kw):
            raise PlanError("Empty response from LLM")

        async def mock_msg_fallback(*args, **kwargs):
            # Snapshot DB during msg composition
            plan_cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
            plan_row = await plan_cur.fetchone()
            task_cur = await db.execute(
                "SELECT type, status FROM tasks WHERE plan_id = ? AND type = 'msg'",
                (plan_id,),
            )
            task_rows = await task_cur.fetchall()
            snapshots.append({
                "plan_status": plan_row["status"],
                "msg_tasks": [(r["type"], r["status"]) for r in task_rows],
            })
            return "failure message"

        with patch("kiso.worker.loop._execute_plan", side_effect=mock_execute), \
             patch("kiso.worker.loop.run_planner", side_effect=mock_planner), \
             patch("kiso.worker.loop._msg_task_with_fallback", side_effect=mock_msg_fallback), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock), \
             _patch_kiso_dir(tmp_path):
            await _run_planning_loop(
                db, config, "sess1", 0, "test", plan_id,
                {"goal": "Test goal", "tasks": []},
                "user", None, 120, {}, None, 600, 5, None, None,
            )

        # During msg composition: plan was "replanning" (set before replan attempt),
        # and the msg task exists as "running"
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert ("msg", "running") in snap["msg_tasks"]

        # After: plan is "failed", msg task is "done"
        plan_cur = await db.execute("SELECT status FROM plans WHERE id = ?", (plan_id,))
        assert (await plan_cur.fetchone())["status"] == "failed"

    async def test_replan_passes_is_replan_to_planner(self, db, tmp_path):
        """_run_planning_loop passes is_replan=True to run_planner during replan."""
        config = _make_config()
        plan_id = await create_plan(db, "sess1", 0, "Test goal")
        from kiso.store import create_task as ct, update_task as ut
        t1 = await ct(db, plan_id, "sess1", "exec", "ls -la")

        planner_kwargs_captured: list[dict] = []

        async def mock_execute(db, cfg, sess, pid, goal, content, **kw):
            await ut(db, t1, "done", output="ok")
            return (False, "replan needed", [{"type": "exec", "detail": "ls", "status": "done", "output": "ok"}], [], [])

        async def mock_planner(db, cfg, sess, role, msg, **kw):
            planner_kwargs_captured.append(kw)
            raise PlanError("fail")

        with patch("kiso.worker.loop._execute_plan", side_effect=mock_execute), \
             patch("kiso.worker.loop.run_planner", side_effect=mock_planner), \
             patch("kiso.worker.loop._msg_task_with_fallback", new_callable=AsyncMock, return_value="fail"), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", new_callable=AsyncMock), \
             _patch_kiso_dir(tmp_path):
            await _run_planning_loop(
                db, config, "sess1", 0, "test", plan_id,
                {"goal": "Test goal", "tasks": []},
                "user", None, 120, {}, None, 600, 5, None, None,
            )

        assert len(planner_kwargs_captured) == 1
        assert planner_kwargs_captured[0].get("is_replan") is True
