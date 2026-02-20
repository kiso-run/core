"""Tests for kiso/brain.py — planner brain."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import aiosqlite
from kiso.brain import (
    CuratorError,
    ExecTranslatorError,
    MessengerError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    _default_messenger_prompt,
    _default_planner_prompt,
    _default_reviewer_prompt,
    _EXEC_TRANSLATOR_PROMPT,
    _load_system_prompt,
    _strip_fences,
    build_curator_messages,
    build_exec_translator_messages,
    build_messenger_messages,
    build_paraphraser_messages,
    build_planner_messages,
    build_reviewer_messages,
    build_summarizer_messages,
    run_curator,
    run_exec_translator,
    run_fact_consolidation,
    run_messenger,
    run_paraphraser,
    run_planner,
    run_reviewer,
    run_summarizer,
    validate_curator,
    validate_plan,
    validate_review,
)
from kiso.config import Config, Provider, KISO_DIR
from kiso.llm import LLMError
from kiso.store import (
    create_session,
    save_message,
    init_db,
)


# --- validate_plan ---

class TestValidatePlan:
    def test_valid_plan(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "files listed", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_empty_tasks(self):
        errors = validate_plan({"tasks": []})
        assert any("not be empty" in e for e in errors)

    def test_missing_tasks_key(self):
        errors = validate_plan({})
        assert any("not be empty" in e for e in errors)

    def test_exec_without_expect(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": None},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("exec task must have a non-null expect" in e for e in errors)

    def test_skill_without_expect(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": None, "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("skill task must have a non-null expect" in e for e in errors)

    def test_msg_with_expect(self):
        plan = {"tasks": [
            {"type": "msg", "detail": "done", "expect": "something"},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must have expect = null" in e for e in errors)

    def test_last_task_not_msg(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "ok"},
        ]}
        errors = validate_plan(plan)
        assert any("Last task must be type 'msg'" in e for e in errors)

    def test_multiple_errors(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": None},
            {"type": "exec", "detail": "pwd", "expect": None},
        ]}
        errors = validate_plan(plan)
        # Two exec-without-expect + last-not-msg
        assert len(errors) >= 3

    def test_single_msg_task_valid(self):
        plan = {"tasks": [
            {"type": "msg", "detail": "Hello!", "expect": None},
        ]}
        assert validate_plan(plan) == []

    def test_exec_with_expect_valid(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "echo hi", "expect": "prints hi"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        assert validate_plan(plan) == []

    # --- M7: skill validation in validate_plan ---

    def test_skill_name_required(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "do thing", "expect": "ok", "skill": None, "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("skill task must have a non-null skill name" in e for e in errors)

    def test_skill_not_installed(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=["echo"])
        assert any("skill 'search' is not installed" in e for e in errors)

    def test_skill_installed_passes(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=["search"])
        assert errors == []

    def test_skill_no_installed_list_skips_check(self):
        """When installed_skills is None, skip skill-not-installed check."""
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=None)
        assert errors == []

    def test_unknown_task_type_rejected(self):
        """Plan with type='query' should produce an error."""
        plan = {"tasks": [
            {"type": "query", "detail": "search", "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("unknown type" in e for e in errors)

    def test_none_task_type_rejected(self):
        """Plan with type=None should produce an error."""
        plan = {"tasks": [
            {"detail": "search", "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("unknown type" in e for e in errors)

    def test_plan_too_many_tasks_rejected(self):
        """Plan with 25 tasks, max_tasks=20, should produce an error."""
        tasks = [
            {"type": "exec", "detail": f"cmd-{i}", "expect": "ok"}
            for i in range(24)
        ] + [{"type": "msg", "detail": "done", "expect": None}]
        plan = {"tasks": tasks}
        errors = validate_plan(plan, max_tasks=20)
        assert any("max allowed is 20" in e for e in errors)

    def test_plan_exactly_at_max_tasks_accepted(self):
        """Plan with exactly max_tasks tasks should pass."""
        tasks = [
            {"type": "exec", "detail": f"cmd-{i}", "expect": "ok"}
            for i in range(19)
        ] + [{"type": "msg", "detail": "done", "expect": None}]
        plan = {"tasks": tasks}
        errors = validate_plan(plan, max_tasks=20)
        assert not any("max allowed" in e for e in errors)


# --- _load_system_prompt ---

class TestLoadSystemPrompt:
    def test_default_when_no_file(self):
        with patch.object(type(KISO_DIR / "roles" / "planner.md"), "exists", return_value=False):
            prompt = _load_system_prompt("planner")
        assert prompt == _default_planner_prompt()
        assert "task planner" in prompt

    def test_reads_file_when_exists(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "planner.md").write_text("Custom prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("planner")
        assert prompt == "Custom prompt"


# --- build_planner_messages ---

class TestBuildPlannerMessages:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"planner": "gpt-4"},
            settings={"context_messages": 3},
            raw={},
        )

    async def test_basic_no_context(self, db, config):
        await create_session(db, "sess1")
        msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "## Caller Role\nadmin" in msgs[1]["content"]
        assert "## New Message" in msgs[1]["content"]
        assert "hello" in msgs[1]["content"]
        assert "<<<USER_MSG_" in msgs[1]["content"]

    async def test_includes_summary(self, db, config):
        await create_session(db, "sess1")
        await db.execute("UPDATE sessions SET summary = 'previous context' WHERE session = 'sess1'")
        await db.commit()
        msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Session Summary" in msgs[1]["content"]
        assert "previous context" in msgs[1]["content"]

    async def test_includes_facts(self, db, config):
        await create_session(db, "sess1")
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Python 3.12", "curator"))
        await db.commit()
        msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Known Facts" in msgs[1]["content"]
        assert "Python 3.12" in msgs[1]["content"]

    async def test_includes_pending(self, db, config):
        await create_session(db, "sess1")
        await db.execute(
            "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
            ("Which DB?", "sess1", "curator"),
        )
        await db.commit()
        msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Pending Questions" in msgs[1]["content"]
        assert "Which DB?" in msgs[1]["content"]

    async def test_includes_recent_messages(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "first msg")
        await save_message(db, "sess1", "alice", "user", "second msg")
        msgs = await build_planner_messages(db, config, "sess1", "admin", "third")
        assert "## Recent Messages" in msgs[1]["content"]
        assert "first msg" in msgs[1]["content"]
        assert "second msg" in msgs[1]["content"]

    async def test_respects_context_limit(self, db, config):
        """Only last context_messages (3) messages are included."""
        await create_session(db, "sess1")
        for i in range(5):
            await save_message(db, "sess1", "alice", "user", f"msg-{i}")
        msgs = await build_planner_messages(db, config, "sess1", "admin", "new")
        content = msgs[1]["content"]
        # Only last 3 should be present
        assert "msg-0" not in content
        assert "msg-1" not in content
        assert "msg-2" in content
        assert "msg-3" in content
        assert "msg-4" in content

    async def test_excludes_untrusted_from_recent(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "trusted", "user", "good msg", trusted=True)
        await save_message(db, "sess1", "stranger", "user", "bad msg", trusted=False, processed=True)
        msgs = await build_planner_messages(db, config, "sess1", "admin", "new")
        content = msgs[1]["content"]
        assert "good msg" in content
        assert "bad msg" not in content

    async def test_no_session_doesnt_crash(self, db, config):
        """Building context for a nonexistent session should not crash."""
        msgs = await build_planner_messages(db, config, "nonexistent", "admin", "hello")
        assert len(msgs) == 2

    # --- M7: skills in planner context ---

    async def test_includes_skills_when_present(self, db, config):
        await create_session(db, "sess1")
        fake_skills = [
            {"name": "search", "summary": "Web search", "args_schema": {
                "query": {"type": "string", "required": True, "description": "search query"},
            }, "env": {}, "session_secrets": [], "path": "/fake", "version": "0.1.0", "description": ""},
        ]
        with patch("kiso.brain.discover_skills", return_value=fake_skills):
            msgs = await build_planner_messages(db, config, "sess1", "admin", "search for X")
        content = msgs[1]["content"]
        assert "## Skills" in content
        assert "search — Web search" in content
        assert "query (string, required): search query" in content

    # --- System environment in planner context ---

    async def test_includes_system_environment(self, db, config):
        """Planner context includes ## System Environment with OS and binaries."""
        await create_session(db, "sess1")
        fake_env = {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.1.0"},
            "shell": "/bin/sh",
            "exec_cwd": "~/.kiso/sessions/{session}/",
            "exec_env": "PATH only (all other env vars stripped)",
            "exec_timeout": 120,
            "max_output_size": 1_048_576,
            "available_binaries": ["git", "python3"],
            "missing_binaries": ["docker"],
            "connectors": [],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
        }
        with patch("kiso.brain.get_system_env", return_value=fake_env):
            msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "## System Environment" in content
        assert "Linux x86_64" in content
        assert "git, python3" in content

    async def test_system_env_after_facts_before_pending(self, db, config):
        """System Environment section appears between Known Facts and Pending Questions."""
        await create_session(db, "sess1")
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Python 3.12", "curator"))
        await db.execute(
            "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
            ("Which DB?", "sess1", "curator"),
        )
        await db.commit()
        fake_env = {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.1.0"},
            "shell": "/bin/sh",
            "exec_cwd": "~/.kiso/sessions/{session}/",
            "exec_env": "PATH only (all other env vars stripped)",
            "exec_timeout": 120,
            "max_output_size": 1_048_576,
            "available_binaries": ["git"],
            "missing_binaries": [],
            "connectors": [],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
        }
        with patch("kiso.brain.get_system_env", return_value=fake_env):
            msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        facts_pos = content.index("## Known Facts")
        sysenv_pos = content.index("## System Environment")
        pending_pos = content.index("## Pending Questions")
        assert facts_pos < sysenv_pos < pending_pos

    async def test_no_skills_section_when_empty(self, db, config):
        await create_session(db, "sess1")
        with patch("kiso.brain.discover_skills", return_value=[]):
            msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "## Skills" not in content

    async def test_user_skills_filtered(self, db, config):
        await create_session(db, "sess1")
        fake_skills = [
            {"name": "search", "summary": "Search", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake", "version": "0.1.0", "description": ""},
            {"name": "aider", "summary": "Code edit", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake2", "version": "0.1.0", "description": ""},
        ]
        with patch("kiso.brain.discover_skills", return_value=fake_skills):
            msgs = await build_planner_messages(
                db, config, "sess1", "user", "hello", user_skills=["search"],
            )
        content = msgs[1]["content"]
        assert "search" in content
        assert "aider" not in content


# --- run_planner ---

VALID_PLAN = json.dumps({
    "goal": "Say hello",
    "secrets": None,
    "tasks": [{"type": "msg", "detail": "Hello!", "skill": None, "args": None, "expect": None}],
})

INVALID_PLAN = json.dumps({
    "goal": "Bad plan",
    "secrets": None,
    "tasks": [{"type": "exec", "detail": "ls", "skill": None, "args": None, "expect": None}],
})


class TestRunPlanner:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"planner": "gpt-4"},
            settings={"max_validation_retries": 3, "context_messages": 5},
            raw={},
        )

    async def test_valid_plan_first_try(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_PLAN):
            plan = await run_planner(db, config, "sess1", "admin", "hello")
        assert plan["goal"] == "Say hello"
        assert len(plan["tasks"]) == 1

    async def test_retry_on_invalid_then_valid(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=[INVALID_PLAN, VALID_PLAN]):
            plan = await run_planner(db, config, "sess1", "admin", "hello")
        assert plan["goal"] == "Say hello"

    async def test_all_retries_exhausted(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=INVALID_PLAN):
            with pytest.raises(PlanError, match="validation failed after 3"):
                await run_planner(db, config, "sess1", "admin", "hello")

    async def test_llm_error_raises_plan_error(self, db, config):
        from kiso.llm import LLMError
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(PlanError, match="LLM call failed.*API down"):
                await run_planner(db, config, "sess1", "admin", "hello")

    async def test_invalid_json_raises_plan_error(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json at all"):
            with pytest.raises(PlanError, match="invalid JSON"):
                await run_planner(db, config, "sess1", "admin", "hello")

    async def test_retry_appends_error_feedback(self, db, config):
        """On retry, error feedback and previous assistant response are appended."""
        call_messages = []

        async def _capture_call(cfg, role, messages, **kw):
            call_messages.append(len(messages))
            if len(call_messages) == 1:
                return INVALID_PLAN
            return VALID_PLAN

        with patch("kiso.brain.call_llm", side_effect=_capture_call):
            await run_planner(db, config, "sess1", "admin", "hello")

        # First call: system + user = 2 messages
        assert call_messages[0] == 2
        # Second call: +assistant (bad plan) +user (error feedback) = 4
        assert call_messages[1] == 4


# --- _load_system_prompt (reviewer) ---

class TestLoadSystemPromptReviewer:
    def test_reviewer_default_when_no_file(self):
        with patch.object(type(KISO_DIR / "roles" / "reviewer.md"), "exists", return_value=False):
            prompt = _load_system_prompt("reviewer")
        assert prompt == _default_reviewer_prompt()
        assert "task reviewer" in prompt

    def test_reviewer_reads_file_when_exists(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "reviewer.md").write_text("Custom reviewer prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("reviewer")
        assert prompt == "Custom reviewer prompt"

    def test_unknown_role_falls_back_to_planner(self):
        with patch.object(type(KISO_DIR / "roles" / "unknown.md"), "exists", return_value=False):
            prompt = _load_system_prompt("unknown")
        assert prompt == _default_planner_prompt()


# --- validate_review ---

class TestValidateReview:
    def test_ok_valid(self):
        review = {"status": "ok", "reason": None, "learn": None}
        assert validate_review(review) == []

    def test_ok_with_learn(self):
        review = {"status": "ok", "reason": None, "learn": "Uses pytest"}
        assert validate_review(review) == []

    def test_replan_with_reason(self):
        review = {"status": "replan", "reason": "File not found", "learn": None}
        assert validate_review(review) == []

    def test_replan_with_reason_and_learn(self):
        review = {"status": "replan", "reason": "Wrong path", "learn": "Project is in /opt"}
        assert validate_review(review) == []

    def test_replan_without_reason_invalid(self):
        review = {"status": "replan", "reason": None, "learn": None}
        errors = validate_review(review)
        assert any("non-null, non-empty reason" in e for e in errors)

    def test_replan_empty_reason_invalid(self):
        review = {"status": "replan", "reason": "", "learn": None}
        errors = validate_review(review)
        assert any("non-null, non-empty reason" in e for e in errors)

    def test_invalid_status(self):
        review = {"status": "maybe", "reason": None, "learn": None}
        errors = validate_review(review)
        assert any("must be 'ok' or 'replan'" in e for e in errors)

    def test_missing_status(self):
        review = {"reason": None, "learn": None}
        errors = validate_review(review)
        assert len(errors) >= 1


# --- build_reviewer_messages ---

class TestBuildReviewerMessages:
    async def test_basic_structure(self):
        msgs = await build_reviewer_messages(
            goal="Test goal",
            detail="echo hello",
            expect="prints hello",
            output="hello\n",
            user_message="run echo hello",
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    async def test_contains_all_context(self):
        msgs = await build_reviewer_messages(
            goal="List files",
            detail="ls -la",
            expect="shows files",
            output="file1.txt\nfile2.txt",
            user_message="list directory contents",
        )
        content = msgs[1]["content"]
        assert "## Plan Goal" in content
        assert "List files" in content
        assert "## Task Detail" in content
        assert "ls -la" in content
        assert "## Expected Outcome" in content
        assert "shows files" in content
        assert "## Actual Output" in content
        assert "file1.txt" in content
        assert "## Original User Message" in content
        assert "list directory contents" in content

    async def test_output_fenced(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="some output",
            user_message="msg",
        )
        content = msgs[1]["content"]
        assert "<<<TASK_OUTPUT_" in content
        assert "some output" in content

    async def test_uses_reviewer_system_prompt(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
        )
        assert "task reviewer" in msgs[0]["content"]

    async def test_custom_reviewer_prompt(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "reviewer.md").write_text("My custom reviewer")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            msgs = await build_reviewer_messages(
                goal="g", detail="d", expect="e", output="o", user_message="m",
            )
        assert msgs[0]["content"] == "My custom reviewer"

    # --- 21e: success param in reviewer context ---

    async def test_success_true_shows_succeeded(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=True,
        )
        content = msgs[1]["content"]
        assert "## Command Status" in content
        assert "succeeded (exit code 0)" in content

    async def test_success_false_shows_failed(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False,
        )
        content = msgs[1]["content"]
        assert "## Command Status" in content
        assert "FAILED (non-zero exit code)" in content

    async def test_success_none_no_command_status(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=None,
        )
        content = msgs[1]["content"]
        assert "## Command Status" not in content

    async def test_success_default_no_command_status(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
        )
        content = msgs[1]["content"]
        assert "## Command Status" not in content


# --- run_reviewer ---

VALID_REVIEW_OK = json.dumps({"status": "ok", "reason": None, "learn": None})
VALID_REVIEW_REPLAN = json.dumps({"status": "replan", "reason": "File missing", "learn": None})
VALID_REVIEW_WITH_LEARN = json.dumps({"status": "ok", "reason": None, "learn": "Uses Python 3.12"})
INVALID_REVIEW = json.dumps({"status": "replan", "reason": None, "learn": None})


class TestRunReviewer:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"reviewer": "gpt-4"},
            settings={"max_validation_retries": 3},
            raw={},
        )

    async def test_ok_first_try(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_OK):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["status"] == "ok"
        assert review["reason"] is None
        assert review["learn"] is None

    async def test_replan_first_try(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_REPLAN):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["status"] == "replan"
        assert review["reason"] == "File missing"

    async def test_review_with_learning(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_WITH_LEARN):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["learn"] == "Uses Python 3.12"

    async def test_retry_on_invalid_then_valid(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=[INVALID_REVIEW, VALID_REVIEW_REPLAN]):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["status"] == "replan"
        assert review["reason"] == "File missing"

    async def test_all_retries_exhausted(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=INVALID_REVIEW):
            with pytest.raises(ReviewError, match="validation failed after 3"):
                await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

    async def test_llm_error_raises_review_error(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(ReviewError, match="LLM call failed.*API down"):
                await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

    async def test_invalid_json_raises_review_error(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            with pytest.raises(ReviewError, match="invalid JSON"):
                await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

    async def test_retry_appends_error_feedback(self, config):
        """On retry, error feedback and previous assistant response are appended."""
        call_messages = []

        async def _capture_call(cfg, role, messages, **kw):
            call_messages.append(len(messages))
            if len(call_messages) == 1:
                return INVALID_REVIEW
            return VALID_REVIEW_REPLAN

        with patch("kiso.brain.call_llm", side_effect=_capture_call):
            await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

        # First call: system + user = 2 messages
        assert call_messages[0] == 2
        # Second call: +assistant (bad review) +user (error feedback) = 4
        assert call_messages[1] == 4

    async def test_passes_review_schema(self, config):
        """run_reviewer passes REVIEW_SCHEMA as response_format."""
        from kiso.brain import REVIEW_SCHEMA

        captured_kwargs = {}

        async def _capture(cfg, role, messages, **kw):
            captured_kwargs.update(kw)
            return VALID_REVIEW_OK

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

        assert captured_kwargs["response_format"] == REVIEW_SCHEMA


# --- M9: validate_curator ---

class TestValidateCurator:
    def test_promote_valid(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "question": None, "reason": "Good fact"},
        ]}
        assert validate_curator(result) == []

    def test_ask_valid(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "ask", "fact": None, "question": "Which DB?", "reason": "Need clarity"},
        ]}
        assert validate_curator(result) == []

    def test_discard_valid(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "discard", "fact": None, "question": None, "reason": "Transient"},
        ]}
        assert validate_curator(result) == []

    def test_promote_missing_fact(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": None, "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result)
        assert any("promote verdict requires a non-empty fact" in e for e in errors)

    def test_ask_missing_question(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "ask", "fact": None, "question": None, "reason": "Need info"},
        ]}
        errors = validate_curator(result)
        assert any("ask verdict requires a non-empty question" in e for e in errors)

    def test_missing_reason(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "discard", "fact": None, "question": None, "reason": ""},
        ]}
        errors = validate_curator(result)
        assert any("reason is required" in e for e in errors)

    def test_multiple_evaluations(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Fact A", "question": None, "reason": "Good"},
            {"learning_id": 2, "verdict": "discard", "fact": None, "question": None, "reason": "Noise"},
            {"learning_id": 3, "verdict": "ask", "fact": None, "question": "What DB?", "reason": "Unclear"},
        ]}
        assert validate_curator(result) == []

    def test_validate_curator_wrong_count(self):
        """Returns error when evaluation count doesn't match expected."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Fact", "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result, expected_count=3)
        assert any("Expected 3 evaluations, got 1" in e for e in errors)

    def test_validate_curator_no_count_check(self):
        """No error when expected_count is None (backwards compat)."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Fact", "question": None, "reason": "Good"},
        ]}
        assert validate_curator(result, expected_count=None) == []


# --- M9: build_curator_messages ---

class TestBuildCuratorMessages:
    def test_formats_learnings(self):
        learnings = [
            {"id": 1, "content": "Uses Flask"},
            {"id": 2, "content": "Database is PostgreSQL"},
        ]
        msgs = build_curator_messages(learnings)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "[id=1] Uses Flask" in msgs[1]["content"]
        assert "[id=2] Database is PostgreSQL" in msgs[1]["content"]

    def test_uses_curator_system_prompt(self):
        msgs = build_curator_messages([{"id": 1, "content": "test"}])
        assert "knowledge curator" in msgs[0]["content"]


# --- M9: run_curator ---

VALID_CURATOR = json.dumps({"evaluations": [
    {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "question": None, "reason": "Good"},
]})

INVALID_CURATOR = json.dumps({"evaluations": [
    {"learning_id": 1, "verdict": "promote", "fact": None, "question": None, "reason": "Good"},
]})


class TestRunCurator:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"curator": "gpt-4"},
            settings={"max_validation_retries": 3},
            raw={},
        )

    async def test_success(self, config):
        learnings = [{"id": 1, "content": "Uses Python"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_CURATOR):
            result = await run_curator(config, learnings)
        assert len(result["evaluations"]) == 1
        assert result["evaluations"][0]["verdict"] == "promote"

    async def test_validation_retry(self, config):
        learnings = [{"id": 1, "content": "Uses Python"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=[INVALID_CURATOR, VALID_CURATOR]):
            result = await run_curator(config, learnings)
        assert result["evaluations"][0]["fact"] == "Uses Python"

    async def test_llm_error_raises_curator_error(self, config):
        learnings = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(CuratorError, match="LLM call failed"):
                await run_curator(config, learnings)

    async def test_all_retries_exhausted(self, config):
        learnings = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=INVALID_CURATOR):
            with pytest.raises(CuratorError, match="validation failed after 3"):
                await run_curator(config, learnings)

    async def test_invalid_json_raises_curator_error(self, config):
        learnings = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            with pytest.raises(CuratorError, match="invalid JSON"):
                await run_curator(config, learnings)


# --- M9: build_summarizer_messages ---

class TestBuildSummarizerMessages:
    def test_includes_summary_and_messages(self):
        messages = [
            {"role": "user", "user": "alice", "content": "Hello"},
            {"role": "system", "content": "Hi there"},
        ]
        msgs = build_summarizer_messages("Previous summary", messages)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert "## Current Summary" in msgs[1]["content"]
        assert "Previous summary" in msgs[1]["content"]
        assert "## Messages" in msgs[1]["content"]
        assert "Hello" in msgs[1]["content"]

    def test_no_summary_omits_section(self):
        messages = [{"role": "user", "user": "alice", "content": "Hello"}]
        msgs = build_summarizer_messages("", messages)
        assert "## Current Summary" not in msgs[1]["content"]
        assert "## Messages" in msgs[1]["content"]


# --- M9: run_summarizer ---

class TestRunSummarizer:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"summarizer": "gpt-4"},
            settings={},
            raw={},
        )

    async def test_success(self, config):
        messages = [{"role": "user", "user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="Updated summary"):
            result = await run_summarizer(config, "Old summary", messages)
        assert result == "Updated summary"

    async def test_llm_error_raises_summarizer_error(self, config):
        messages = [{"role": "user", "user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(SummarizerError, match="LLM call failed"):
                await run_summarizer(config, "", messages)


# --- M9: run_fact_consolidation ---

class TestRunFactConsolidation:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"summarizer": "gpt-4"},
            settings={},
            raw={},
        )

    async def test_returns_list_of_strings(self, config):
        facts = [
            {"id": 1, "content": "Uses Python"},
            {"id": 2, "content": "Uses Python 3.12"},
        ]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value='["Uses Python 3.12"]'):
            result = await run_fact_consolidation(config, facts)
        assert result == ["Uses Python 3.12"]

    async def test_llm_error_raises_summarizer_error(self, config):
        facts = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("down")):
            with pytest.raises(SummarizerError, match="LLM call failed"):
                await run_fact_consolidation(config, facts)

    async def test_invalid_json_raises_error(self, config):
        facts = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            with pytest.raises(SummarizerError, match="invalid JSON"):
                await run_fact_consolidation(config, facts)

    async def test_non_array_raises_error(self, config):
        facts = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value='{"not": "array"}'):
            with pytest.raises(SummarizerError, match="must return a JSON array"):
                await run_fact_consolidation(config, facts)


# --- M9: _load_system_prompt for curator/summarizer ---

class TestLoadSystemPromptCuratorSummarizer:
    def test_curator_default(self):
        with patch.object(type(KISO_DIR / "roles" / "curator.md"), "exists", return_value=False):
            prompt = _load_system_prompt("curator")
        assert "knowledge curator" in prompt

    def test_summarizer_default(self):
        with patch.object(type(KISO_DIR / "roles" / "summarizer.md"), "exists", return_value=False):
            prompt = _load_system_prompt("summarizer")
        assert "session summarizer" in prompt

    def test_paraphraser_default(self):
        with patch.object(type(KISO_DIR / "roles" / "paraphraser.md"), "exists", return_value=False):
            prompt = _load_system_prompt("paraphraser")
        assert "paraphraser" in prompt


# --- M10: Paraphraser ---

class TestBuildParaphraserMessages:
    def test_formats_messages(self):
        messages = [
            {"user": "alice", "content": "Hello there"},
            {"user": "bob", "content": "How are you?"},
        ]
        msgs = build_paraphraser_messages(messages)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "[alice]: Hello there" in msgs[1]["content"]
        assert "[bob]: How are you?" in msgs[1]["content"]

    def test_missing_user_defaults_unknown(self):
        messages = [{"content": "test message"}]
        msgs = build_paraphraser_messages(messages)
        assert "[unknown]: test message" in msgs[1]["content"]


class TestRunParaphraser:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"paraphraser": "gpt-4"},
            settings={},
            raw={},
        )

    async def test_run_paraphraser_success(self, config):
        messages = [{"user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="The user greeted the assistant."):
            result = await run_paraphraser(config, messages)
        assert result == "The user greeted the assistant."

    async def test_run_paraphraser_error(self, config):
        messages = [{"user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(ParaphraserError, match="LLM call failed"):
                await run_paraphraser(config, messages)


# --- M10: Fencing in planner messages ---

class TestPlannerMessagesFencing:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models={"planner": "gpt-4"},
            settings={"context_messages": 3},
            raw={},
        )

    async def test_planner_messages_fence_recent(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "hello world")
        msgs = await build_planner_messages(db, config, "sess1", "admin", "new msg")
        content = msgs[1]["content"]
        assert "<<<MESSAGES_" in content
        assert "<<<END_MESSAGES_" in content

    async def test_planner_messages_fence_new_message(self, db, config):
        await create_session(db, "sess1")
        msgs = await build_planner_messages(db, config, "sess1", "admin", "test input")
        content = msgs[1]["content"]
        assert "<<<USER_MSG_" in content
        assert "<<<END_USER_MSG_" in content
        assert "test input" in content

    async def test_planner_messages_include_paraphrased(self, db, config):
        await create_session(db, "sess1")
        msgs = await build_planner_messages(
            db, config, "sess1", "admin", "hello",
            paraphrased_context="The external user asked about the weather.",
        )
        content = msgs[1]["content"]
        assert "## Paraphrased External Messages (untrusted)" in content
        assert "<<<PARAPHRASED_" in content
        assert "The external user asked about the weather." in content

    async def test_planner_messages_no_paraphrased_when_none(self, db, config):
        await create_session(db, "sess1")
        msgs = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "Paraphrased" not in content


# --- M10: Fencing in reviewer messages ---

class TestReviewerMessagesFencing:
    async def test_reviewer_messages_fence_output(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="some task output",
            user_message="user msg",
        )
        content = msgs[1]["content"]
        assert "<<<TASK_OUTPUT_" in content
        assert "<<<END_TASK_OUTPUT_" in content
        assert "some task output" in content

    async def test_reviewer_messages_fence_user_message(self):
        msgs = await build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="output",
            user_message="the original user message",
        )
        content = msgs[1]["content"]
        assert "<<<USER_MSG_" in content
        assert "<<<END_USER_MSG_" in content
        assert "the original user message" in content


# --- _strip_fences ---


class TestStripFences:
    def test_no_fences(self):
        assert _strip_fences('{"key": "value"}') == '{"key": "value"}'

    def test_json_fence(self):
        assert _strip_fences('```json\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_plain_fence(self):
        assert _strip_fences('```\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_leading_whitespace(self):
        assert _strip_fences(' ```json\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_trailing_whitespace(self):
        assert _strip_fences('```json\n{"key": "value"}\n``` ') == '{"key": "value"}'

    def test_bare_json(self):
        raw = '{"goal": "test", "secrets": null, "tasks": []}'
        assert _strip_fences(raw) == raw

    def test_empty_string(self):
        assert _strip_fences('') == ''


# --- Messenger ---

def _make_brain_config(**overrides) -> Config:
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"local": Provider(base_url="http://localhost:11434/v1")},
        users={},
        models={"messenger": "gpt-4"},
        settings={"bot_name": "Kiso"},
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestDefaultMessengerPrompt:
    def test_contains_placeholder(self):
        prompt = _default_messenger_prompt()
        assert "{bot_name}" in prompt

    def test_load_replaces_bot_name(self):
        config = _make_brain_config(settings={"bot_name": "TestBot"})
        msgs = build_messenger_messages(config, "", [], "say hi")
        system_prompt = msgs[0]["content"]
        assert "TestBot" in system_prompt
        assert "{bot_name}" not in system_prompt


class TestBuildMessengerMessages:
    def test_basic_structure(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "## Task\nsay hi" in msgs[1]["content"]

    def test_includes_summary(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "User is working on Flask app", [], "say hi")
        assert "Flask app" in msgs[1]["content"]

    def test_includes_facts(self):
        config = _make_brain_config()
        facts = [{"content": "Uses Python 3.12"}]
        msgs = build_messenger_messages(config, "", facts, "say hi")
        assert "Python 3.12" in msgs[1]["content"]

    def test_includes_plan_outputs(self):
        config = _make_brain_config()
        outputs_text = "[1] exec: echo hi\nStatus: done\nhi"
        msgs = build_messenger_messages(config, "", [], "report", outputs_text)
        assert "## Preceding Task Outputs" in msgs[1]["content"]
        assert "echo hi" in msgs[1]["content"]

    def test_no_outputs_section_when_empty(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi", "")
        assert "Preceding Task Outputs" not in msgs[1]["content"]


class TestRunMessenger:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_successful_call(self, db):
        config = _make_brain_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="Ciao!"):
            result = await run_messenger(db, config, "sess1", "Greet the user")
        assert result == "Ciao!"

    async def test_uses_messenger_role(self, db):
        config = _make_brain_config()
        captured = {}

        async def _capture(cfg, role, messages, **kw):
            captured["role"] = role
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(db, config, "sess1", "say hi")
        assert captured["role"] == "messenger"

    async def test_llm_error_raises_messenger_error(self, db):
        config = _make_brain_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(MessengerError, match="API down"):
                await run_messenger(db, config, "sess1", "say hi")

    async def test_loads_custom_role_file(self, db, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "messenger.md").write_text("You are {bot_name}, a pirate assistant.")
        config = _make_brain_config(settings={"bot_name": "Arrr"})
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture), \
             patch("kiso.brain.KISO_DIR", tmp_path):
            await run_messenger(db, config, "sess1", "say hi")

        assert "Arrr" in captured_messages[0]["content"]
        assert "pirate" in captured_messages[0]["content"]

    async def test_custom_role_without_placeholder(self, db, tmp_path):
        """Custom messenger.md without {bot_name} should work fine."""
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "messenger.md").write_text("You are a helpful robot.")
        config = _make_brain_config(settings={"bot_name": "Kiso"})
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture), \
             patch("kiso.brain.KISO_DIR", tmp_path):
            await run_messenger(db, config, "sess1", "say hi")

        assert "helpful robot" in captured_messages[0]["content"]
        assert "{bot_name}" not in captured_messages[0]["content"]


class TestLoadSystemPromptMessenger:
    def test_default_messenger_prompt(self):
        with patch.object(type(KISO_DIR / "roles" / "messenger.md"), "exists", return_value=False):
            prompt = _load_system_prompt("messenger")
        assert "{bot_name}" in prompt
        assert "friendly" in prompt


# --- Exec Translator ---

class TestBuildExecTranslatorMessages:
    def test_basic_structure(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List all Python files", "OS: Linux\nShell: /bin/sh",
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "## Task\nList all Python files" in msgs[1]["content"]
        assert "## System Environment" in msgs[1]["content"]

    def test_includes_plan_outputs(self):
        config = _make_brain_config()
        outputs_text = "[1] exec: list files\nStatus: done\nfoo.py"
        msgs = build_exec_translator_messages(
            config, "Count them", "OS: Linux", outputs_text,
        )
        assert "## Preceding Task Outputs" in msgs[1]["content"]
        assert "foo.py" in msgs[1]["content"]

    def test_no_outputs_section_when_empty(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List files", "OS: Linux", "",
        )
        assert "Preceding Task Outputs" not in msgs[1]["content"]


class TestRunExecTranslator:
    async def test_successful_translation(self):
        config = _make_brain_config(models={"worker": "gpt-4"})
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="ls -la *.py"):
            result = await run_exec_translator(
                config, "List all Python files", "OS: Linux",
            )
        assert result == "ls -la *.py"

    async def test_strips_whitespace(self):
        config = _make_brain_config(models={"worker": "gpt-4"})
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="  ls -la  \n"):
            result = await run_exec_translator(
                config, "List files", "OS: Linux",
            )
        assert result == "ls -la"

    async def test_cannot_translate_raises(self):
        config = _make_brain_config(models={"worker": "gpt-4"})
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="CANNOT_TRANSLATE"):
            with pytest.raises(ExecTranslatorError, match="Cannot translate"):
                await run_exec_translator(config, "Do something impossible", "OS: Linux")

    async def test_empty_result_raises(self):
        config = _make_brain_config(models={"worker": "gpt-4"})
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="   "):
            with pytest.raises(ExecTranslatorError, match="Cannot translate"):
                await run_exec_translator(config, "Do something", "OS: Linux")

    async def test_llm_error_raises_translator_error(self):
        config = _make_brain_config(models={"worker": "gpt-4"})
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(ExecTranslatorError, match="API down"):
                await run_exec_translator(config, "List files", "OS: Linux")

    async def test_uses_worker_role(self):
        config = _make_brain_config(models={"worker": "gpt-4"})
        captured = {}

        async def _capture(cfg, role, messages, **kw):
            captured["role"] = role
            return "echo hello"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_exec_translator(config, "Say hello", "OS: Linux")
        assert captured["role"] == "worker"


class TestLoadSystemPromptExecTranslator:
    def test_default_prompt(self):
        with patch.object(type(KISO_DIR / "roles" / "exec_translator.md"), "exists", return_value=False):
            prompt = _load_system_prompt("exec_translator")
        assert "shell command translator" in prompt
        assert "CANNOT_TRANSLATE" in prompt

    def test_custom_prompt(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "exec_translator.md").write_text("Custom exec translator")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("exec_translator")
        assert prompt == "Custom exec translator"


# --- Exec translator prompt content ---


class TestExecTranslatorPromptContent:
    def test_exec_translator_prompt_mentions_preceding_outputs(self):
        """The default exec translator prompt should mention Preceding Task Outputs."""
        assert "Preceding Task Outputs" in _EXEC_TRANSLATOR_PROMPT
