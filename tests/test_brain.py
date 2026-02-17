"""Tests for kiso/brain.py — planner brain."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import aiosqlite
from kiso.brain import (
    PlanError,
    ReviewError,
    _default_planner_prompt,
    _default_reviewer_prompt,
    _load_system_prompt,
    build_planner_messages,
    build_reviewer_messages,
    run_planner,
    run_reviewer,
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
        assert "## New Message\nhello" in msgs[1]["content"]

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
        assert "```\nsome output\n```" in content

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
