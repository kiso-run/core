"""Tests for kiso/brain.py — planner brain."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import jsonschema as _jsonschema
import pytest

import aiosqlite
from kiso.brain import (
    ClassifierError,
    CURATOR_SCHEMA,
    CuratorError,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    ExecTranslatorError,
    MessengerError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    _load_system_prompt,
    _prompt_cache,
    _ROLES_DIR,
    invalidate_prompt_cache,
    _strip_fences,
    _repair_json,
    _is_plugin_discovery_search,
    build_classifier_messages,
    build_curator_messages,
    build_exec_translator_messages,
    build_messenger_messages,
    build_paraphraser_messages,
    build_planner_messages,
    build_reviewer_messages,
    build_summarizer_messages,
    classify_message,
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
from kiso.config import Config, Provider, KISO_DIR, SETTINGS_DEFAULTS, MODEL_DEFAULTS


def _full_settings(**overrides) -> dict:
    """Return a complete settings dict (all required keys) with optional overrides."""
    return {**SETTINGS_DEFAULTS, **overrides}


def _full_models(**overrides) -> dict:
    """Return a complete models dict with optional overrides."""
    return {**MODEL_DEFAULTS, **overrides}
from kiso.llm import LLMError
from kiso.store import (
    create_session,
    save_message,
    init_db,
)


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """Ensure the prompt cache is clean before and after every test."""
    invalidate_prompt_cache()
    yield
    invalidate_prompt_cache()


# --- Worker phase constants (M109c) ---


def test_worker_phases_frozenset():
    """WORKER_PHASES contains all four phase constants."""
    from kiso.brain import (
        WORKER_PHASE_CLASSIFYING,
        WORKER_PHASE_EXECUTING,
        WORKER_PHASE_IDLE,
        WORKER_PHASE_PLANNING,
        WORKER_PHASES,
    )
    assert WORKER_PHASES == frozenset({
        WORKER_PHASE_CLASSIFYING, WORKER_PHASE_PLANNING,
        WORKER_PHASE_EXECUTING, WORKER_PHASE_IDLE,
    })
    assert len(WORKER_PHASES) == 4


# --- M111d: reviewer prompt hardening ---


def test_reviewer_prompt_contains_empty_output_guard():
    """M111d: reviewer.md must instruct LLM to set learn=null on empty output."""
    prompt = _load_system_prompt("reviewer")
    assert "empty or whitespace-only" in prompt
    assert "learn MUST be null" in prompt


def test_reviewer_prompt_contains_reason_required_rule():
    """M111d: reviewer.md must require reason for replan status."""
    prompt = _load_system_prompt("reviewer")
    assert "required (non-null, non-empty string) when status is replan" in prompt


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

    def test_msg_with_non_null_skill(self):
        """M84i: msg task with skill != null must fail validation."""
        plan = {"tasks": [
            {"type": "msg", "detail": "done", "expect": None, "skill": "my-skill", "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must have skill = null" in e for e in errors)

    def test_msg_with_non_null_args(self):
        """M84i: msg task with args != null must fail validation."""
        plan = {"tasks": [
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": '{"key": "val"}'},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must have args = null" in e for e in errors)

    def test_last_task_not_msg(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "ok"},
        ]}
        errors = validate_plan(plan)
        assert any("Last task must be type 'msg' or 'replan'" in e for e in errors)

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
        assert any("Available skills: echo" in e for e in errors)

    def test_skill_not_installed_empty_list(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=[])
        assert any("Available skills: none" in e for e in errors)

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

    # --- M25: replan task type ---

    def test_replan_as_last_task_valid(self):
        """Plan with exec + replan → valid."""
        plan = {"tasks": [
            {"type": "exec", "detail": "read registry", "expect": "JSON output", "skill": None, "args": None},
            {"type": "replan", "detail": "install appropriate skill", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_replan_not_last_task_invalid(self):
        """Replan followed by msg → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": None, "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("replan task can only be the last task" in e for e in errors)

    def test_replan_with_expect_invalid(self):
        """Replan task with non-null expect → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": "something", "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("replan task must have expect = null" in e for e in errors)

    def test_replan_with_skill_invalid(self):
        """Replan task with non-null skill → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": None, "skill": "search", "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("replan task must have skill = null" in e for e in errors)

    def test_replan_with_args_invalid(self):
        """Replan task with non-null args → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": None, "skill": None, "args": "{}"},
        ]}
        errors = validate_plan(plan)
        assert any("replan task must have args = null" in e for e in errors)

    def test_multiple_replan_tasks_invalid(self):
        """Two replan tasks → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "first", "expect": None, "skill": None, "args": None},
            {"type": "replan", "detail": "second", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("at most one replan task" in e for e in errors)

    def test_replan_only_plan_valid(self):
        """Plan with only a replan task → valid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate first", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_extend_replan_field_accepted(self):
        """Plan with extend_replan=2 → valid (extend_replan is a plan-level field, not validated in validate_plan)."""
        plan = {
            "extend_replan": 2,
            "tasks": [
                {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
            ],
        }
        assert validate_plan(plan) == []

    def test_last_task_must_be_msg_or_replan(self):
        """Plan ending with exec (not msg or replan) → invalid."""
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "ok"},
        ]}
        errors = validate_plan(plan)
        assert any("Last task must be type 'msg' or 'replan'" in e for e in errors)

    # --- M31: search task type ---

    def test_search_task_valid(self):
        """search + expect, skill=null → valid."""
        plan = {"tasks": [
            {"type": "search", "detail": "best restaurants in Milan", "expect": "list of restaurants", "skill": None, "args": None},
            {"type": "msg", "detail": "present results", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_search_task_with_args(self):
        """search + args JSON string → valid."""
        plan = {"tasks": [
            {"type": "search", "detail": "best SEO agencies", "expect": "list of agencies", "skill": None, "args": '{"max_results": 10, "lang": "it", "country": "IT"}'},
            {"type": "msg", "detail": "present results", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_search_task_missing_expect(self):
        """search without expect → error."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": None, "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search task must have a non-null expect" in e for e in errors)

    def test_search_task_with_skill(self):
        """search with skill set → error."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": "search", "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search task must have skill = null" in e for e in errors)

    def test_search_task_not_last(self):
        """Plan ending with search → error (last must be msg or replan)."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("Last task must be type 'msg' or 'replan'" in e for e in errors)

    def test_plan_search_then_msg(self):
        """search + msg → valid."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": None, "args": None},
            {"type": "msg", "detail": "present results", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_plan_search_then_replan(self):
        """search + replan → valid (investigation pattern)."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": None, "args": None},
            {"type": "replan", "detail": "plan next steps", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []


# --- _load_system_prompt ---

class TestLoadSystemPrompt:
    def test_package_default_when_no_user_file(self):
        prompt = _load_system_prompt("planner")
        assert "planner" in prompt

    def test_user_override_takes_priority(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "planner.md").write_text("Custom prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("planner")
        assert prompt == "Custom prompt"

    def test_unknown_role_raises(self, tmp_path):
        # Use a tmp_path with no roles dir as KISO_DIR to avoid class-wide mock
        with patch("kiso.brain.KISO_DIR", tmp_path):
            with pytest.raises(FileNotFoundError, match="No prompt found for role 'nonexistent'"):
                _load_system_prompt("nonexistent")


# --- _load_system_prompt — cache (M65b) ---

class TestLoadSystemPromptCache:
    def test_result_is_cached_on_second_call(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        role_file = roles_dir / "planner.md"
        role_file.write_text("v1")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            first = _load_system_prompt("planner")
        # Overwrite file — cached value should still be returned
        role_file.write_text("v2")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            second = _load_system_prompt("planner")
        assert first == "v1"
        assert second == "v1"  # still cached

    def test_invalidate_prompt_cache_clears_cache(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        role_file = roles_dir / "planner.md"
        role_file.write_text("v1")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            _load_system_prompt("planner")
        assert "planner" in _prompt_cache
        invalidate_prompt_cache()
        assert "planner" not in _prompt_cache

    def test_after_invalidation_file_is_reread(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        role_file = roles_dir / "planner.md"
        role_file.write_text("v1")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            _load_system_prompt("planner")
        role_file.write_text("v2")
        invalidate_prompt_cache()
        with patch("kiso.brain.KISO_DIR", tmp_path):
            result = _load_system_prompt("planner")
        assert result == "v2"


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
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(context_messages=3),
            raw={},
        )

    async def test_basic_no_context(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
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
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Session Summary" in msgs[1]["content"]
        assert "previous context" in msgs[1]["content"]

    async def test_includes_facts(self, db, config):
        await create_session(db, "sess1")
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Python 3.12", "curator"))
        await db.commit()
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Known Facts" in msgs[1]["content"]
        assert "Python 3.12" in msgs[1]["content"]

    async def test_facts_grouped_by_category(self, db, config):
        """Facts are grouped by category in planner context."""
        await create_session(db, "sess1")
        from kiso.store import save_fact
        await save_fact(db, "Uses Flask", "curator", category="project")
        await save_fact(db, "Prefers dark mode", "curator", category="user")
        await save_fact(db, "Git available", "curator", category="tool")
        await save_fact(db, "Some general fact", "curator", category="general")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "### Project" in content
        assert "### User" in content
        assert "### Tool" in content
        assert "### General" in content
        # Verify order: project before user before tool before general
        proj_pos = content.index("### Project")
        user_pos = content.index("### User")
        tool_pos = content.index("### Tool")
        gen_pos = content.index("### General")
        assert proj_pos < user_pos < tool_pos < gen_pos

    async def test_admin_facts_hierarchy(self, db, config):
        """M44f: admin context shows current-session+global facts in ## Known Facts (primary)
        and other-session facts in ## Context from Other Sessions (background)."""
        from kiso.store import save_fact
        await create_session(db, "sess1")
        await create_session(db, "sess-other")
        await save_fact(db, "Alice prefers verbose", "curator", session="sess1", category="user")
        await save_fact(db, "Bob prefers brief", "curator", session="sess-other", category="user")
        await save_fact(db, "Uses Docker", "curator")  # no session — global
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        # Current session + global go into primary block, no session label
        assert "## Known Facts" in content
        assert "Alice prefers verbose" in content
        assert "Uses Docker" in content
        known_pos = content.index("## Known Facts")
        assert "Alice prefers verbose" in content[known_pos:]
        assert "Uses Docker" in content[known_pos:]
        # Other sessions go into secondary block, with session label
        assert "## Context from Other Sessions" in content
        other_pos = content.index("## Context from Other Sessions")
        assert "Bob prefers brief [session:sess-other]" in content[other_pos:]
        # Current-session fact must NOT carry a session label
        assert "Alice prefers verbose [session:" not in content

    async def test_non_admin_facts_no_session_labels(self, db, config):
        """M44f: non-admin planner context never shows session labels or the other-sessions block."""
        from kiso.store import save_fact
        await create_session(db, "sess1")
        await save_fact(db, "Uses pytest", "curator", session="sess1", category="tool")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "user", "hello")
        content = msgs[1]["content"]
        assert "Uses pytest" in content
        assert "[session:" not in content
        assert "## Context from Other Sessions" not in content

    async def test_unknown_category_falls_back_to_general(self, db, config):
        """Facts with unknown categories appear under ### General."""
        await create_session(db, "sess1")
        from kiso.store import save_fact
        await save_fact(db, "Exotic fact", "curator", category="exotic")
        await save_fact(db, "Normal fact", "curator", category="general")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "### General" in content
        assert "Exotic fact" in content
        # Unknown category should NOT get its own heading
        assert "### Exotic" not in content

    async def test_includes_pending(self, db, config):
        await create_session(db, "sess1")
        await db.execute(
            "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
            ("Which DB?", "sess1", "curator"),
        )
        await db.commit()
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Pending Questions" in msgs[1]["content"]
        assert "Which DB?" in msgs[1]["content"]

    async def test_includes_recent_messages(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "first msg")
        await save_message(db, "sess1", "alice", "user", "second msg")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "third")
        assert "## Recent Messages" in msgs[1]["content"]
        assert "first msg" in msgs[1]["content"]
        assert "second msg" in msgs[1]["content"]

    async def test_respects_context_limit(self, db, config):
        """Only last context_messages (3) messages are included."""
        await create_session(db, "sess1")
        for i in range(5):
            await save_message(db, "sess1", "alice", "user", f"msg-{i}")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "new")
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
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "new")
        content = msgs[1]["content"]
        assert "good msg" in content
        assert "bad msg" not in content

    async def test_no_session_doesnt_crash(self, db, config):
        """Building context for a nonexistent session should not crash."""
        msgs, _installed = await build_planner_messages(db, config, "nonexistent", "admin", "hello")
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
            msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "search for X")
        content = msgs[1]["content"]
        assert "## Skills" in content
        assert "search — Web search" in content
        assert "query (string, required): search query" in content

    # --- System environment in planner context ---

    async def test_includes_system_environment(self, db, config):
        """Planner context includes ## System Environment with OS and binaries."""
        await create_session(db, "sess1")
        from kiso.config import KISO_DIR
        fake_env = {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.1.0"},
            "shell": "/bin/sh",
            "exec_cwd": str(KISO_DIR / "sessions"),
            "exec_env": "PATH (sys/bin prepended) + HOME + git/ssh env vars when config exists",
            "exec_timeout": 120,
            "max_output_size": 1_048_576,
            "available_binaries": ["git", "python3"],
            "missing_binaries": ["docker"],
            "connectors": [],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
            "sys_bin_path": str(KISO_DIR / "sys" / "bin"),
            "reference_docs_path": str(KISO_DIR / "reference"),
            "registry_url": "https://raw.githubusercontent.com/kiso-run/core/main/registry.json",
        }
        with patch("kiso.brain.get_system_env", return_value=fake_env):
            msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "## System Environment" in content
        assert "Linux x86_64" in content
        assert "git, python3" in content
        # Session name should be included in the system env section
        assert "Session: sess1" in content
        expected_cwd = str(KISO_DIR / "sessions" / "sess1")
        assert f"Exec CWD: {expected_cwd}" in content

    async def test_system_env_after_facts_before_pending(self, db, config):
        """System Environment section appears between Known Facts and Pending Questions."""
        await create_session(db, "sess1")
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Python 3.12", "curator"))
        await db.execute(
            "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
            ("Which DB?", "sess1", "curator"),
        )
        await db.commit()
        from kiso.config import KISO_DIR
        fake_env = {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.1.0"},
            "shell": "/bin/sh",
            "exec_cwd": str(KISO_DIR / "sessions"),
            "exec_env": "PATH (sys/bin prepended) + HOME + git/ssh env vars when config exists",
            "exec_timeout": 120,
            "max_output_size": 1_048_576,
            "available_binaries": ["git"],
            "missing_binaries": [],
            "connectors": [],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
            "sys_bin_path": str(KISO_DIR / "sys" / "bin"),
            "reference_docs_path": str(KISO_DIR / "reference"),
            "registry_url": "https://raw.githubusercontent.com/kiso-run/core/main/registry.json",
        }
        with patch("kiso.brain.get_system_env", return_value=fake_env):
            msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        facts_pos = content.index("## Known Facts")
        sysenv_pos = content.index("## System Environment")
        pending_pos = content.index("## Pending Questions")
        assert facts_pos < sysenv_pos < pending_pos

    async def test_no_skills_section_when_empty(self, db, config):
        await create_session(db, "sess1")
        with patch("kiso.brain.discover_skills", return_value=[]):
            msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
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
            msgs, _installed = await build_planner_messages(
                db, config, "sess1", "user", "hello", user_skills=["search"],
            )
        content = msgs[1]["content"]
        assert "search" in content
        assert "aider" not in content

    async def test_logs_warning_when_no_skills(self, db, config, caplog):
        """M3: build_planner_messages logs warning when discover_skills returns empty."""
        import logging
        await create_session(db, "sess1")
        with (
            patch("kiso.brain.discover_skills", return_value=[]),
            caplog.at_level(logging.WARNING, logger="kiso.brain"),
        ):
            msgs, names = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert names == []
        assert "discover_skills() returned empty" in caplog.text


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
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3, context_messages=5),
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
            with pytest.raises(PlanError, match="(?i)invalid JSON"):
                await run_planner(db, config, "sess1", "admin", "hello")

    async def test_invalid_json_retries_before_raising(self, db, config):
        """M84b: JSON parse error should retry, not raise immediately."""
        call_count = 0

        async def _bad_then_good(cfg, role, messages, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "not json at all"
            return VALID_PLAN

        with patch("kiso.brain.call_llm", side_effect=_bad_then_good):
            plan = await run_planner(db, config, "sess1", "admin", "hello")

        assert call_count == 2, "Expected retry after JSON error"
        assert plan["goal"] == "Say hello"

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
        prompt = _load_system_prompt("reviewer")
        assert "task reviewer" in prompt

    def test_reviewer_reads_file_when_exists(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "reviewer.md").write_text("Custom reviewer prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("reviewer")
        assert prompt == "Custom reviewer prompt"


# --- validate_review ---

class TestValidateReview:
    def test_ok_valid(self):
        review = {"status": "ok", "reason": None, "learn": None}
        assert validate_review(review) == []

    def test_ok_with_learn(self):
        review = {"status": "ok", "reason": None, "learn": ["Uses pytest"]}
        assert validate_review(review) == []

    def test_replan_with_reason(self):
        review = {"status": "replan", "reason": "File not found", "learn": None}
        assert validate_review(review) == []

    def test_replan_with_reason_and_learn(self):
        review = {"status": "replan", "reason": "Wrong path", "learn": ["Project is in /opt"]}
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


# --- M83: JSON-Schema validation for PLAN_SCHEMA and REVIEW_SCHEMA ---


_PLAN_SCHEMA_INNER = PLAN_SCHEMA["json_schema"]["schema"]
_REVIEW_SCHEMA_INNER = REVIEW_SCHEMA["json_schema"]["schema"]
_MSG_TASK_DICT = {"type": "msg", "detail": "Hello", "skill": None, "args": None, "expect": None}


class TestM83PlanSchema:
    """M83: PLAN_SCHEMA inner schema accepts valid plans and rejects invalid ones."""

    def _valid(self, instance):
        _jsonschema.validate(instance=instance, schema=_PLAN_SCHEMA_INNER)

    def _invalid(self, instance):
        with pytest.raises(_jsonschema.ValidationError):
            _jsonschema.validate(instance=instance, schema=_PLAN_SCHEMA_INNER)

    def _plan(self, **overrides):
        base = {"goal": "Do X", "secrets": None, "tasks": [{**_MSG_TASK_DICT}], "extend_replan": None}
        base.update(overrides)
        return base

    # Valid ---

    def test_valid_minimal(self):
        self._valid(self._plan())

    def test_valid_secrets_array(self):
        self._valid(self._plan(secrets=[{"key": "K", "value": "V"}]))

    def test_valid_extend_replan_integer(self):
        self._valid(self._plan(extend_replan=3))

    @pytest.mark.parametrize("t", ["exec", "msg", "skill", "search", "replan"])
    def test_valid_task_type(self, t):
        self._valid(self._plan(tasks=[{"type": t, "detail": "x", "skill": None, "args": None, "expect": None}]))

    # Invalid ---

    def test_missing_goal(self):
        d = self._plan()
        del d["goal"]
        self._invalid(d)

    def test_missing_extend_replan(self):
        d = self._plan()
        del d["extend_replan"]
        self._invalid(d)

    def test_extra_top_level_field(self):
        self._invalid(self._plan(unexpected="boom"))

    def test_task_invalid_type_enum(self):
        self._invalid(self._plan(tasks=[{"type": "fly", "detail": "x", "skill": None, "args": None, "expect": None}]))

    def test_task_missing_required_field(self):
        # missing "expect"
        self._invalid(self._plan(tasks=[{"type": "msg", "detail": "x", "skill": None, "args": None}]))

    def test_task_extra_field(self):
        self._invalid(self._plan(tasks=[{**_MSG_TASK_DICT, "extra": "x"}]))

    def test_extend_replan_wrong_type(self):
        self._invalid(self._plan(extend_replan="three"))


class TestM83ReviewSchema:
    """M83: REVIEW_SCHEMA inner schema accepts valid reviews and rejects invalid ones."""

    def _valid(self, instance):
        _jsonschema.validate(instance=instance, schema=_REVIEW_SCHEMA_INNER)

    def _invalid(self, instance):
        with pytest.raises(_jsonschema.ValidationError):
            _jsonschema.validate(instance=instance, schema=_REVIEW_SCHEMA_INNER)

    # Valid ---

    def test_valid_ok_nulls(self):
        self._valid({"status": "ok", "reason": None, "learn": None, "retry_hint": None})

    def test_valid_replan_full(self):
        self._valid({"status": "replan", "reason": "wrong path", "learn": ["Fact 1"], "retry_hint": "try /opt"})

    def test_valid_learn_exactly_5(self):
        self._valid({"status": "ok", "reason": None, "learn": ["a", "b", "c", "d", "e"], "retry_hint": None})

    # Invalid ---

    def test_missing_status(self):
        self._invalid({"reason": None, "learn": None, "retry_hint": None})

    def test_status_not_in_enum(self):
        self._invalid({"status": "maybe", "reason": None, "learn": None, "retry_hint": None})

    def test_missing_retry_hint(self):
        self._invalid({"status": "ok", "reason": None, "learn": None})

    def test_extra_field(self):
        self._invalid({"status": "ok", "reason": None, "learn": None, "retry_hint": None, "extra": "x"})

    def test_learn_exceeds_max_items(self):
        self._invalid({"status": "ok", "reason": None, "learn": ["a", "b", "c", "d", "e", "f"], "retry_hint": None})

    def test_learn_non_string_item(self):
        self._invalid({"status": "ok", "reason": None, "learn": [42], "retry_hint": None})


# --- build_reviewer_messages ---

class TestBuildReviewerMessages:
    def test_is_sync_function(self):
        """M66e: build_reviewer_messages must be a plain def, not async def."""
        import inspect
        assert not inspect.iscoroutinefunction(build_reviewer_messages), (
            "build_reviewer_messages has no awaits and must be a regular function"
        )

    def test_returns_list_directly(self):
        """Calling without await returns a list immediately (not a coroutine)."""
        import inspect
        result = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m"
        )
        assert not inspect.iscoroutine(result), "Must not return a coroutine"
        assert isinstance(result, list)

    async def test_basic_structure(self):
        msgs = build_reviewer_messages(
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
        msgs = build_reviewer_messages(
            goal="List files",
            detail="ls -la",
            expect="shows files",
            output="file1.txt\nfile2.txt",
            user_message="list directory contents",
        )
        content = msgs[1]["content"]
        assert "## Plan Context" in content
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
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="some output",
            user_message="msg",
        )
        content = msgs[1]["content"]
        assert "<<<TASK_OUTPUT_" in content
        assert "some output" in content

    async def test_uses_reviewer_system_prompt(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
        )
        assert "task reviewer" in msgs[0]["content"]

    async def test_custom_reviewer_prompt(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "reviewer.md").write_text("My custom reviewer")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            msgs = build_reviewer_messages(
                goal="g", detail="d", expect="e", output="o", user_message="m",
            )
        assert msgs[0]["content"] == "My custom reviewer"

    # --- 21e: success param in reviewer context ---

    async def test_success_true_shows_succeeded(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=True,
        )
        content = msgs[1]["content"]
        assert "## Command Status" in content
        assert "succeeded (exit code 0)" in content

    async def test_success_false_shows_failed(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False,
        )
        content = msgs[1]["content"]
        assert "## Command Status" in content
        assert "FAILED (non-zero exit code)" in content

    async def test_success_none_no_command_status(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=None,
        )
        content = msgs[1]["content"]
        assert "## Command Status" not in content

    async def test_success_default_no_command_status(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
        )
        content = msgs[1]["content"]
        assert "## Command Status" not in content

    # --- M106: exit_code parameter ---

    async def test_exit_code_0_shows_success(self):
        """exit_code=0 with success=True shows 'Exit code: 0 (success)'."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=True, exit_code=0,
        )
        content = msgs[1]["content"]
        assert "Exit code: 0 (success)" in content

    async def test_exit_code_1_shows_note(self):
        """exit_code=1 includes note about grep/which/find."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=1,
        )
        content = msgs[1]["content"]
        assert "Exit code: 1 (non-zero)" in content
        assert "no matches found" in content

    async def test_exit_code_127_shows_note(self):
        """exit_code=127 includes note about command not found."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=127,
        )
        content = msgs[1]["content"]
        assert "Exit code: 127 (non-zero)" in content
        assert "not found in PATH" in content

    async def test_exit_code_126_shows_note(self):
        """exit_code=126 includes note about permission issue."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=126,
        )
        content = msgs[1]["content"]
        assert "Exit code: 126 (non-zero)" in content
        assert "not executable" in content

    async def test_exit_code_neg1_shows_timeout_note(self):
        """exit_code=-1 includes note about timeout/OS error."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=-1,
        )
        content = msgs[1]["content"]
        assert "Exit code: -1 (non-zero)" in content
        assert "killed" in content.lower()

    async def test_exit_code_2_shows_syntax_note(self):
        """exit_code=2 includes note about usage/syntax error."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=2,
        )
        content = msgs[1]["content"]
        assert "Exit code: 2 (non-zero)" in content
        assert "usage" in content.lower() or "syntax" in content.lower()

    async def test_exit_code_unknown_no_note(self):
        """Unknown exit code (e.g. 42) shows code but no note."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=42,
        )
        content = msgs[1]["content"]
        assert "Exit code: 42 (non-zero)" in content
        assert "Note:" not in content

    async def test_exit_code_none_backward_compat(self):
        """exit_code=None (default) falls back to old format."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=None,
        )
        content = msgs[1]["content"]
        assert "FAILED (non-zero exit code)" in content


# --- run_reviewer ---

VALID_REVIEW_OK = json.dumps({"status": "ok", "reason": None, "learn": None, "retry_hint": None})
VALID_REVIEW_REPLAN = json.dumps({"status": "replan", "reason": "File missing", "learn": None, "retry_hint": None})
VALID_REVIEW_WITH_LEARN = json.dumps({"status": "ok", "reason": None, "learn": ["Uses Python 3.12"], "retry_hint": None})
INVALID_REVIEW = json.dumps({"status": "replan", "reason": None, "learn": None, "retry_hint": None})


class TestRunReviewer:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(reviewer="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
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

    async def test_exit_code_forwarded_to_messages(self, config):
        """run_reviewer forwards exit_code to build_reviewer_messages."""
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return VALID_REVIEW_OK

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_reviewer(
                config, "goal", "detail", "expect", "output", "msg",
                success=False, exit_code=127,
            )
        user_content = captured_messages[1]["content"]
        assert "Exit code: 127" in user_content
        assert "not found in PATH" in user_content

    async def test_review_with_learning(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_WITH_LEARN):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["learn"] == ["Uses Python 3.12"]

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
            with pytest.raises(ReviewError, match="(?i)invalid JSON"):
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
    {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": "project", "question": None, "reason": "Good"},
]})

INVALID_CURATOR = json.dumps({"evaluations": [
    {"learning_id": 1, "verdict": "promote", "fact": None, "category": None, "question": None, "reason": "Good"},
]})


class TestRunCurator:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(curator="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
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
            with pytest.raises(CuratorError, match="(?i)invalid JSON"):
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
            models=_full_models(summarizer="gpt-4"),
            settings=_full_settings(),
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
            models=_full_models(summarizer="gpt-4"),
            settings=_full_settings(),
            raw={},
        )

    async def test_returns_list_of_dicts(self, config):
        facts = [
            {"id": 1, "content": "Uses Python"},
            {"id": 2, "content": "Uses Python 3.12"},
        ]
        llm_response = json.dumps([
            {"content": "Uses Python 3.12", "category": "project", "confidence": 1.0}
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "Uses Python 3.12"
        assert result[0]["category"] == "project"
        assert result[0]["confidence"] == 1.0

    async def test_backward_compat_plain_strings(self, config):
        """LLM returns plain strings → wrapped into dicts with defaults."""
        facts = [
            {"id": 1, "content": "Uses Python"},
            {"id": 2, "content": "Uses Python 3.12"},
        ]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value='["Uses Python 3.12"]'):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "Uses Python 3.12"
        assert result[0]["category"] == "general"
        assert result[0]["confidence"] == 1.0

    async def test_dict_with_defaults(self, config):
        """Dict with only content key → category and confidence get defaults."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([{"content": "test fact"}])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["category"] == "general"
        assert result[0]["confidence"] == 1.0

    async def test_invalid_items_skipped(self, config):
        """Items that are not dicts or strings are skipped."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "valid", "category": "project", "confidence": 0.9},
            42,
            None,
            {"no_content_key": True},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "valid"

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

    async def test_confidence_non_numeric_string_falls_back(self, config):
        """M84d: non-numeric confidence string must fall back to 1.0 without crashing."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "Uses Python", "confidence": "high"},
            {"content": "Uses Linux", "confidence": None},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 2
        assert result[0]["confidence"] == 1.0
        assert result[1]["confidence"] == 1.0

    async def test_confidence_clamped_to_unit_interval(self, config):
        """M37: confidence values outside [0.0, 1.0] are clamped."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "high", "confidence": 99.9},
            {"content": "low", "confidence": -5.0},
            {"content": "normal", "confidence": 0.7},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["confidence"] == 1.0   # clamped from 99.9
        assert result[1]["confidence"] == 0.0   # clamped from -5.0
        assert result[2]["confidence"] == 0.7   # unchanged

    async def test_item_count_cap_truncates_oversized_result(self, config):
        """M87e: LLM returning more than _MAX_CONSOLIDATION_ITEMS items is truncated."""
        from kiso.brain import _MAX_CONSOLIDATION_ITEMS
        facts = [{"id": 1, "content": "test"}]
        oversized = [{"content": f"fact {i}", "category": "general"} for i in range(_MAX_CONSOLIDATION_ITEMS + 50)]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=json.dumps(oversized)):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == _MAX_CONSOLIDATION_ITEMS

    async def test_unknown_category_normalized_to_general(self, config):
        """M87e: unrecognized category strings are normalized to 'general'."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "fact with bad category", "category": "injected"},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["category"] == "general"

    async def test_null_category_normalized_to_general(self, config):
        """M87e: null category is normalized to 'general'."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "fact with null category", "category": None},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["category"] == "general"

    async def test_empty_and_whitespace_content_filtered(self, config):
        """M87e: items with empty or whitespace-only content are filtered out."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "valid fact"},
            {"content": ""},
            {"content": "  "},
            {"content": "ab"},
            " ",
            "",
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "valid fact"

    async def test_valid_categories_preserved(self, config):
        """M87e: all four valid categories are accepted as-is."""
        from kiso.brain import _VALID_FACT_CATEGORIES
        facts = [{"id": 1, "content": "test"}]
        items = [{"content": f"fact for {cat}", "category": cat} for cat in sorted(_VALID_FACT_CATEGORIES)]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=json.dumps(items)):
            result = await run_fact_consolidation(config, facts)
        result_cats = {r["category"] for r in result}
        assert result_cats == _VALID_FACT_CATEGORIES


# --- M9: _load_system_prompt for curator/summarizer ---

class TestLoadSystemPromptCuratorSummarizer:
    def test_curator_default(self):
        prompt = _load_system_prompt("curator")
        assert "knowledge curator" in prompt

    def test_summarizer_session_default(self):
        prompt = _load_system_prompt("summarizer-session")
        assert "session summarizer" in prompt
        assert "Key Decisions" in prompt
        assert "Open Questions" in prompt
        assert "Working Knowledge" in prompt

    def test_summarizer_facts_default(self):
        prompt = _load_system_prompt("summarizer-facts")
        assert "fact" in prompt.lower()
        assert "category" in prompt.lower()
        assert "confidence" in prompt.lower()

    def test_paraphraser_default(self):
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
            models=_full_models(paraphraser="gpt-4"),
            settings=_full_settings(),
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
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(context_messages=3),
            raw={},
        )

    async def test_planner_messages_fence_recent(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "hello world")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "new msg")
        content = msgs[1]["content"]
        assert "<<<MESSAGES_" in content
        assert "<<<END_MESSAGES_" in content

    async def test_planner_messages_fence_new_message(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "test input")
        content = msgs[1]["content"]
        assert "<<<USER_MSG_" in content
        assert "<<<END_USER_MSG_" in content
        assert "test input" in content

    async def test_planner_messages_include_paraphrased(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed = await build_planner_messages(
            db, config, "sess1", "admin", "hello",
            paraphrased_context="The external user asked about the weather.",
        )
        content = msgs[1]["content"]
        assert "## Paraphrased External Messages (untrusted)" in content
        assert "<<<PARAPHRASED_" in content
        assert "The external user asked about the weather." in content

    async def test_planner_messages_no_paraphrased_when_none(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "Paraphrased" not in content


# --- M10: Fencing in reviewer messages ---

class TestReviewerMessagesFencing:
    async def test_reviewer_messages_fence_output(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="some task output",
            user_message="user msg",
        )
        content = msgs[1]["content"]
        assert "<<<TASK_OUTPUT_" in content
        assert "<<<END_TASK_OUTPUT_" in content
        assert "some task output" in content

    async def test_reviewer_messages_fence_user_message(self):
        msgs = build_reviewer_messages(
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
    base_settings = _full_settings()
    if "settings" in overrides:
        base_settings.update(overrides.pop("settings"))
    base_models = _full_models(messenger="gpt-4")
    if "models" in overrides:
        base_models.update(overrides.pop("models"))
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"local": Provider(base_url="http://localhost:11434/v1")},
        users={},
        models=base_models,
        settings=base_settings,
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestDefaultMessengerPrompt:
    def test_contains_placeholder(self):
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "{bot_name}" in prompt

    def test_verbatim_instructions_rule(self):
        """M46: messenger prompt must instruct to reproduce setup instructions verbatim."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "verbatim" in prompt

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

    def test_includes_goal(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi", goal="List files")
        assert "## Current User Request\nList files" in msgs[1]["content"]

    def test_no_goal_section_when_empty(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi")
        assert "Current User Request" not in msgs[1]["content"]

    def test_goal_appears_before_summary(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "old context", [], "say hi", goal="new question",
        )
        content = msgs[1]["content"]
        goal_pos = content.index("Current User Request")
        summary_pos = content.index("Session Summary")
        assert goal_pos < summary_pos


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

    async def test_goal_passed_to_context(self, db):
        """run_messenger passes goal to build_messenger_messages context."""
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(db, config, "sess1", "say hi", goal="List files")
        user_content = captured_messages[1]["content"]
        assert "Current User Request" in user_content
        assert "List files" in user_content

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
        config = _make_brain_config(settings=_full_settings(bot_name="Kiso"))
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
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="ls -la *.py"):
            result = await run_exec_translator(
                config, "List all Python files", "OS: Linux",
            )
        assert result == "ls -la *.py"

    async def test_strips_whitespace(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="  ls -la  \n"):
            result = await run_exec_translator(
                config, "List files", "OS: Linux",
            )
        assert result == "ls -la"

    async def test_cannot_translate_raises(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="CANNOT_TRANSLATE"):
            with pytest.raises(ExecTranslatorError, match="Cannot translate"):
                await run_exec_translator(config, "Do something impossible", "OS: Linux")

    async def test_empty_result_raises(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="   "):
            with pytest.raises(ExecTranslatorError, match="Cannot translate"):
                await run_exec_translator(config, "Do something", "OS: Linux")

    async def test_llm_error_raises_translator_error(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(ExecTranslatorError, match="API down"):
                await run_exec_translator(config, "List files", "OS: Linux")

    async def test_uses_worker_role(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        captured = {}

        async def _capture(cfg, role, messages, **kw):
            captured["role"] = role
            return "echo hello"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_exec_translator(config, "Say hello", "OS: Linux")
        assert captured["role"] == "worker"


class TestLoadSystemPromptExecTranslator:
    def test_default_prompt(self):
        prompt = _load_system_prompt("worker")
        assert "shell command translator" in prompt
        assert "CANNOT_TRANSLATE" in prompt

    def test_custom_prompt_overrides(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "worker.md").write_text("Custom worker prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("worker")
        assert prompt == "Custom worker prompt"


# --- Exec translator prompt content ---


class TestExecTranslatorPromptContent:
    def test_exec_translator_prompt_mentions_preceding_outputs(self):
        """The default exec translator prompt should mention Preceding Task Outputs."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "Preceding Task Outputs" in prompt


# --- Planner prompt content ---


class TestPlannerPromptContent:
    def test_planner_prompt_contains_reference_docs_instruction(self):
        """The default planner prompt should mention reference docs."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "reference doc" in prompt.lower()
        assert "plan_outputs" in prompt

    def test_planner_prompt_contains_replan_task_type(self):
        """The default planner prompt should mention the replan task type."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "replan" in prompt
        assert "investigation" in prompt.lower() or "investigate" in prompt.lower()
        assert "extend_replan" in prompt

    def test_planner_prompt_contains_search_task_type(self):
        """The default planner prompt should mention the search task type."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "search:" in prompt
        assert "web search" in prompt.lower() or "search query" in prompt.lower()

    def test_m40_last_task_rule_marked_critical(self):
        """M40: last-task rule must be prefixed CRITICAL to reduce model skips."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "CRITICAL:" in prompt

    def test_m40_pub_path_distinction_documented(self):
        """M40: pub/ filesystem path vs served URL must be explicitly distinguished."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "filesystem path" in prompt
        # Files served at URLs — must not use the URL as a path
        assert "never use the url" in prompt.lower() or "served at url" in prompt.lower()

    def test_m45_plugin_install_uses_registry_not_search(self):
        """M45: planner plugin-install appendix must forbid web search for kiso plugin discovery."""
        prompt = (_ROLES_DIR / "planner-plugin-install.md").read_text()
        assert "NEVER use" in prompt or "NEVER" in prompt
        assert "registry" in prompt.lower()
        assert "web search" in prompt.lower()

    def test_m45_plugin_install_rule_is_mandatory(self):
        """M45: plugin installation appendix must be marked MANDATORY."""
        prompt = (_ROLES_DIR / "planner-plugin-install.md").read_text()
        assert "Plugin installation (MANDATORY)" in prompt

    def test_m46_plugin_install_checks_kiso_toml_before_install(self):
        """M46: planner must curl kiso.toml from GitHub before installing to discover env requirements."""
        prompt = (_ROLES_DIR / "planner-plugin-install.md").read_text()
        assert "raw.githubusercontent.com" in prompt
        assert "kiso.toml" in prompt
        # Step ordering: curl kiso.toml (step 3) must come before "kiso connector install {name}" (step 6).
        toml_pos = prompt.index("kiso.toml")
        install_pos = prompt.index("kiso connector install {name}")
        assert toml_pos < install_pos

    def test_m46_plugin_install_includes_env_description_in_msg(self):
        """M46: planner plugin-install appendix must include env var descriptions from kiso.toml."""
        prompt = (_ROLES_DIR / "planner-plugin-install.md").read_text()
        assert "description" in prompt
        assert "how to obtain" in prompt.lower() or "descriptions from kiso.toml" in prompt

    def test_m102a_plugin_discovery_never_single_type_search(self):
        """M102a: planner must NEVER use single-type search for initial plugin discovery."""
        prompt = (_ROLES_DIR / "planner-plugin-install.md").read_text()
        assert "NEVER" in prompt
        assert "kiso connector search" in prompt
        assert "kiso skill search" in prompt
        # The NEVER clause must be in the context of single-type search
        never_idx = prompt.index("NEVER use `kiso connector search`")
        assert "kiso skill search" in prompt[never_idx:never_idx + 200]


class TestM73cPlannerUserManagement:
    """M73c: planner prompt rules for kiso user subcommand (now in appendix files)."""

    def test_kiso_user_commands_listed(self):
        """kiso user commands must appear in the kiso-commands appendix."""
        prompt = (_ROLES_DIR / "planner-kiso-commands.md").read_text()
        assert "kiso user add" in prompt
        assert "kiso user remove" in prompt
        assert "kiso user list" in prompt
        assert "kiso user alias" in prompt

    def test_user_management_admin_only_label(self):
        """The user commands must be labelled as admin only."""
        prompt = (_ROLES_DIR / "planner-kiso-commands.md").read_text()
        idx = prompt.index("kiso user add")
        section = prompt[max(0, idx - 100):idx + 50]
        assert "admin only" in section

    def test_protection_rule_blocks_non_admin(self):
        """Planner user-mgmt appendix must refuse kiso user tasks when Caller Role is 'user'."""
        prompt = (_ROLES_DIR / "planner-user-mgmt.md").read_text()
        assert "Caller Role" in prompt
        assert "NEVER" in prompt
        protection_idx = prompt.index("PROTECTION")
        protection_text = prompt[protection_idx:protection_idx + 200]
        assert "kiso user" in protection_text
        assert "user" in protection_text.lower()

    def test_skills_required_for_role_user(self):
        """User-mgmt appendix must document that --skills is required when role=user."""
        prompt = (_ROLES_DIR / "planner-user-mgmt.md").read_text()
        assert "--skills" in prompt
        assert "--role user" in prompt

    def test_ask_for_role_before_add(self):
        """Planner user-mgmt appendix must ask for role if not specified."""
        prompt = (_ROLES_DIR / "planner-user-mgmt.md").read_text()
        assert "role is not specified" in prompt or "role not specified" in prompt or \
               "role" in prompt and "msg task" in prompt

    def test_ask_for_connector_aliases(self):
        """Planner user-mgmt appendix must ask for connector aliases."""
        prompt = (_ROLES_DIR / "planner-user-mgmt.md").read_text()
        assert "connector" in prompt.lower()
        assert "System Environment" in prompt


# --- M82: planner ask-then-add workflow (functional) ---


_MSG_PLAN_FOR_USER = json.dumps({
    "goal": "Relay user management request to admin",
    "secrets": None,
    "extend_replan": None,
    "tasks": [{
        "type": "msg",
        "detail": "I cannot add users directly. Please ask your admin to run: kiso user add bob --role user",
        "skill": None,
        "args": None,
        "expect": None,
    }],
})


@pytest.mark.asyncio
class TestM82PlannerAskThenAdd:
    """M82: functional tests for the ask-then-add protection workflow.

    When Caller Role=user, a kiso user management request must result in a
    msg task (not exec).  The protection is prompt-based; these tests verify:
    1. build_planner_messages correctly injects Caller Role: user into context.
    2. run_planner accepts a msg-only plan returned by the LLM for this scenario.
    3. The LLM actually sees the Caller Role and the original request together.
    """

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
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3, context_messages=5),
            raw={},
        )

    async def test_caller_role_user_in_messages(self, db, config):
        """build_planner_messages injects '## Caller Role\\nuser' for role=user."""
        msgs, _ = await build_planner_messages(db, config, "sess1", "user", "add user bob")
        assert "## Caller Role\nuser" in msgs[1]["content"]

    async def test_run_planner_accepts_msg_only_plan(self, db, config):
        """run_planner with user_role='user' returns the msg plan without errors."""
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=_MSG_PLAN_FOR_USER):
            plan = await run_planner(db, config, "sess1", "user", "add user bob to kiso")
        assert plan["tasks"][0]["type"] == "msg"
        assert len(plan["tasks"]) == 1

    async def test_llm_sees_caller_role_and_request_together(self, db, config):
        """LLM receives both '## Caller Role\\nuser' and the kiso user add request."""
        captured: list[dict] = []

        async def _capture(cfg, role, messages, **kw):
            captured.extend(messages)
            return _MSG_PLAN_FOR_USER

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_planner(db, config, "sess1", "user", "kiso user add charlie")

        user_msg = next((m for m in captured if m["role"] == "user"), None)
        assert user_msg is not None, "No user message found in LLM call"
        assert "## Caller Role\nuser" in user_msg["content"]
        assert "kiso user add charlie" in user_msg["content"]


# --- Classifier (fast path) ---


def _make_config_for_classifier():
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=_full_models(worker="gpt-3.5"),
        settings=_full_settings(),
        raw={},
    )


class TestBuildClassifierMessages:
    def test_basic_structure(self):
        """build_classifier_messages returns system + user messages."""
        msgs = build_classifier_messages("hello there")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "hello there"

    def test_system_prompt_loaded(self):
        """System prompt should come from classifier.md."""
        msgs = build_classifier_messages("test")
        assert "plan" in msgs[0]["content"]
        assert "chat" in msgs[0]["content"]


class TestClassifyMessage:
    async def test_returns_chat(self):
        """classify_message returns 'chat' when LLM says 'chat'."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="chat"):
            result = await classify_message(config, "hello")
        assert result == "chat"

    async def test_returns_plan(self):
        """classify_message returns 'plan' when LLM says 'plan'."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="plan"):
            result = await classify_message(config, "list files")
        assert result == "plan"

    async def test_strips_whitespace(self):
        """classify_message handles LLM output with whitespace."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="  chat\n"):
            result = await classify_message(config, "thanks")
        assert result == "chat"

    async def test_case_insensitive(self):
        """classify_message handles uppercase responses."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="CHAT"):
            result = await classify_message(config, "thanks")
        assert result == "chat"

    async def test_unexpected_output_falls_back_to_plan(self):
        """classify_message returns 'plan' for unexpected LLM output."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="I think this is a chat"):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_llm_error_falls_back_to_plan(self):
        """classify_message returns 'plan' when LLM call fails."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, side_effect=LLMError("timeout")):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_budget_exceeded_falls_back_to_plan(self):
        """classify_message returns 'plan' when LLM budget is exhausted."""
        from kiso.llm import LLMBudgetExceeded
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, side_effect=LLMBudgetExceeded("over")):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_empty_response_falls_back_to_plan(self):
        """classify_message returns 'plan' when LLM returns empty string."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=""):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_uses_worker_model(self):
        """classify_message should call LLM with 'worker' role."""
        config = _make_config_for_classifier()
        mock_llm = AsyncMock(return_value="chat")
        with patch("kiso.brain.call_llm", mock_llm):
            await classify_message(config, "hello", session="s1")
        mock_llm.assert_called_once()
        assert mock_llm.call_args[0][1] == "worker"  # role argument
        assert mock_llm.call_args[1].get("session") == "s1"


class TestClassifierPromptContent:
    def test_classifier_prompt_exists(self):
        """classifier.md role file should exist."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert len(prompt) > 0

    def test_classifier_prompt_mentions_categories(self):
        """Classifier prompt should define plan and chat categories."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert "plan" in prompt
        assert "chat" in prompt

    def test_classifier_prompt_safe_fallback(self):
        """Classifier prompt should instruct to default to plan when in doubt."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert "doubt" in prompt.lower()
        assert "plan" in prompt


# --- M33: retry_hint in REVIEW_SCHEMA ---


class TestRetryHintInSchema:
    def test_retry_hint_in_review_schema(self):
        """REVIEW_SCHEMA includes retry_hint property."""
        props = REVIEW_SCHEMA["json_schema"]["schema"]["properties"]
        assert "retry_hint" in props
        required = REVIEW_SCHEMA["json_schema"]["schema"]["required"]
        assert "retry_hint" in required

    def test_validate_review_ok_with_retry_hint(self):
        review = {"status": "ok", "reason": None, "learn": None, "retry_hint": None}
        assert validate_review(review) == []

    def test_validate_review_replan_with_retry_hint(self):
        review = {"status": "replan", "reason": "wrong path", "learn": None, "retry_hint": "use /opt/app"}
        assert validate_review(review) == []

    async def test_run_reviewer_returns_retry_hint(self):
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(reviewer="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )
        review_json = json.dumps({
            "status": "replan", "reason": "wrong path",
            "learn": None, "retry_hint": "use /opt/app",
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=review_json):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["retry_hint"] == "use /opt/app"


# --- M33: retry_context in exec translator ---


class TestExecTranslatorRetryContext:
    def test_retry_context_included_in_messages(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List Python files", "OS: Linux",
            retry_context="Previous command failed. Hint: use python3 not python",
        )
        content = msgs[1]["content"]
        assert "## Retry Context" in content
        assert "use python3 not python" in content

    def test_retry_context_empty_not_included(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List Python files", "OS: Linux",
            retry_context="",
        )
        content = msgs[1]["content"]
        assert "Retry Context" not in content

    def test_retry_context_before_task_section(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List Python files", "OS: Linux",
            retry_context="Hint: use python3",
        )
        content = msgs[1]["content"]
        retry_pos = content.index("## Retry Context")
        task_pos = content.index("## Task")
        assert retry_pos < task_pos

    async def test_run_exec_translator_passes_retry_context(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "python3 script.py"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            result = await run_exec_translator(
                config, "Run script", "OS: Linux",
                retry_context="use python3 not python",
            )
        assert result == "python3 script.py"
        user_content = captured_messages[1]["content"]
        assert "## Retry Context" in user_content
        assert "use python3 not python" in user_content


# --- M33: reviewer prompt mentions retry_hint ---


class TestReviewerPromptRetryHint:
    def test_reviewer_prompt_mentions_retry_hint(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "retry_hint" in prompt


# --- M47: planner/reviewer/worker improvements ---


class TestM47PlannerIdentityAndTwoLayer:
    """47a: planner self-awareness — identity + two-layer environment."""

    def test_planner_prompt_mentions_kiso_identity(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Kiso planner" in prompt

    def test_planner_prompt_mentions_two_layers(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "two layers" in prompt

    def test_planner_prompt_prefers_kiso_native_solution(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Kiso-native" in prompt

    def test_planner_prompt_unambiguous_bias(self):
        """Clarification rule flipped: proceed only if unambiguous, else ask."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "unambiguous" in prompt
        assert "When in doubt, ask" in prompt

    def test_planner_prompt_expect_scoping(self):
        """expect must be task-scoped, not plan-goal-scoped."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "THIS task's output" in prompt
        assert "overall plan goal" in prompt


class TestM47ReviewerPlanContext:
    """47b: reviewer receives Plan Context (not Plan Goal) and evaluates only expect."""

    async def test_reviewer_messages_use_plan_context_label(self):
        msgs = build_reviewer_messages(
            goal="Install Discord",
            detail="run apt-get install -f",
            expect="exits 0",
            output="0 upgraded, 0 installed",
            user_message="install discord",
            success=True,
        )
        content = msgs[1]["content"]
        assert "## Plan Context" in content
        assert "## Plan Goal" not in content

    def test_reviewer_prompt_plan_context_is_background(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "background" in prompt and "context" in prompt.lower()

    def test_reviewer_prompt_sole_criterion_is_expect(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "Sole criterion" in prompt

    def test_reviewer_prompt_zero_changes_is_success(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "0 changes" in prompt or "nothing to do" in prompt


# --- M96: reviewer — warning vs error distinction ---


class TestReviewerWarningVsError:
    """M96: warnings don't override exit 0 + satisfied expect."""

    def test_prompt_warnings_are_informational(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "informational" in prompt

    def test_prompt_no_blanket_replan_on_warning(self):
        """Old rule 'mark as replan even if command succeeded' must be gone."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "mark as replan even if the command succeeded" not in prompt

    def test_prompt_explicit_expect_required_for_warning_replan(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "expect" in prompt.lower()
        # The new rule: replan for warnings only if expect requires clean output
        assert "absence of warnings" in prompt or "no warnings" in prompt


class TestM47WorkerHintPriority:
    """47d: worker gives priority to retry hint over literal task detail re-translation."""

    def test_worker_prompt_hint_takes_priority(self):
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "hint" in prompt.lower()
        # Hint must override literal re-translation (various phrasings accepted)
        assert "priority" in prompt.lower() or "over" in prompt.lower()

    def test_retry_context_with_hint_is_visible_to_translator(self):
        """Hint in retry context is present in the messages sent to the exec translator."""
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config,
            detail="Fix remaining dependency issues with apt-get install -f",
            sys_env_text="OS: Linux",
            retry_context="Attempt 1 failed.\nHint: use apt install discord instead",
        )
        content = msgs[1]["content"]
        assert "## Retry Context" in content
        assert "apt install discord" in content

    def test_retry_context_without_hint_still_works(self):
        """Retry context without hint does not break translation."""
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config,
            detail="list files",
            sys_env_text="OS: Linux",
            retry_context="Attempt 1 failed. Command not found.",
        )
        content = msgs[1]["content"]
        assert "## Retry Context" in content
        assert "## Task" in content


# --- M47: edge cases ---


class TestM47ReviewerPlanContextEdgeCases:
    """Edge cases for reviewer Plan Context / expect evaluation."""

    async def test_reviewer_messages_goal_text_present_as_context(self):
        """Goal text is still present in the message — just under Plan Context label."""
        msgs = build_reviewer_messages(
            goal="Install Discord on the system",
            detail="run apt-get install -f",
            expect="exits 0, no broken dependencies (0 changes acceptable)",
            output="0 upgraded, 0 installed, 0 to remove",
            user_message="install discord",
            success=True,
        )
        content = msgs[1]["content"]
        # Goal text is present for context
        assert "Install Discord on the system" in content
        # But under the right label
        assert "## Plan Context" in content

    async def test_reviewer_messages_structure_order(self):
        """Plan Context comes before Task Detail which comes before Expected Outcome."""
        msgs = build_reviewer_messages(
            goal="some goal",
            detail="some detail",
            expect="some expect",
            output="some output",
            user_message="user msg",
        )
        content = msgs[1]["content"]
        plan_ctx_pos = content.index("## Plan Context")
        task_detail_pos = content.index("## Task Detail")
        expected_pos = content.index("## Expected Outcome")
        assert plan_ctx_pos < task_detail_pos < expected_pos

    async def test_reviewer_messages_no_success_flag(self):
        """build_reviewer_messages works without success flag (no Command Status section)."""
        msgs = build_reviewer_messages(
            goal="goal",
            detail="detail",
            expect="expect",
            output="output",
            user_message="msg",
        )
        content = msgs[1]["content"]
        assert "## Plan Context" in content
        assert "Command Status" not in content

    async def test_reviewer_messages_with_success_false(self):
        """Failed exit code is reported correctly."""
        msgs = build_reviewer_messages(
            goal="goal",
            detail="detail",
            expect="expect",
            output="error output",
            user_message="msg",
            success=False,
        )
        content = msgs[1]["content"]
        assert "FAILED" in content or "non-zero" in content


class TestM47PlannerExpectScopingEdgeCases:
    """Edge cases for planner expect scoping guidance."""

    def test_planner_prompt_maintenance_commands_mentioned(self):
        """Prompt explicitly calls out maintenance commands as edge case for expect."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # apt-get install -f or similar maintenance commands should be mentioned
        assert "apt-get install -f" in prompt or "maintenance" in prompt or "cleanup" in prompt

    def test_planner_prompt_clarification_rule_not_just_unclear(self):
        """New rule is more than 'if unclear, ask' — requires unambiguous intent AND target."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # Old rule was simply "if the request is unclear"
        # New rule requires both intent and target to be unambiguous
        assert "intent" in prompt
        assert "target" in prompt


# ---------------------------------------------------------------------------
# M48: Prompt hygiene — ottimizzazioni trasversali ai role prompts
# ---------------------------------------------------------------------------


class TestM48ReviewerPromptHygiene:
    """48a+48b: reviewer prompt alignment and exit code rule."""

    def test_48a_you_receive_says_plan_context(self):
        """48a: reviewer prompt must refer to 'Plan Context', not 'Plan Goal'."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "Plan Context" in prompt

    def test_48a_no_plan_goal_in_receive_block(self):
        """48a: old 'The plan goal' bullet must be gone from 'You receive' section."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "The plan goal" not in prompt

    def test_48b_exit_code_rule_present(self):
        """48b: reviewer prompt must have an explicit rule about exit codes."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "exit code" in prompt.lower() or "non-zero" in prompt.lower()

    def test_48b_nonzero_is_failure_signal(self):
        """48b: non-zero exit code must be labeled as failure indicator."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "non-zero" in prompt.lower()

    def test_48b_zero_not_sufficient_alone(self):
        """48b: zero exit code is necessary but not sufficient — output must also satisfy expect."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "not sufficient" in prompt.lower()


class TestM48PlannerMergedRules:
    """48c: planner expect and detail rules are merged (no redundant duplicates)."""

    def test_48c_expect_rule_is_nonnull_and_task_scoped(self):
        """48c: single expect rule combines 'non-null' and 'task-specific' guidance."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "non-null" in prompt
        assert "not the overall plan goal" in prompt or "task's output alone" in prompt

    def test_48c_no_redundant_standalone_nonnull_rule(self):
        """48c: old fragmented 'non-null expect field' standalone line must be gone."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "exec, skill, and search tasks MUST have a non-null expect field" not in prompt

    def test_48c_detail_rule_is_selfcontained_and_specific(self):
        """48c: single detail rule combines 'self-contained' and 'specific + commands/paths'."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "self-contained" in prompt
        assert "cannot invent or guess" in prompt

    def test_48c_no_redundant_standalone_specific_rule(self):
        """48c: old fragmented 'exec task detail must be specific' standalone line must be gone."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "exec task detail must be specific" not in prompt

    def test_48c_expect_rule_covers_exec_skill_search(self):
        """48c: merged expect rule must apply to exec/skill/search."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # Should mention all three types together
        assert "exec/skill/search" in prompt or ("exec" in prompt and "skill" in prompt and "search" in prompt)

    def test_48c_detail_rule_mentions_exec_commands_and_paths(self):
        """48c: merged detail rule must mention commands/paths for exec tasks."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "commands" in prompt or "paths" in prompt


# --- M97a: planner contextual rule blocks ---


@pytest.mark.asyncio
class TestPlannerContextualRules:
    """M97a: appendix blocks injected only when message keywords match."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "test-session")
        yield conn
        await conn.close()

    def _config(self):
        return _make_brain_config()

    async def test_generic_message_has_no_appendix(self, db):
        """A message like 'what time is it' should not inject any appendix."""
        msgs, _ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "what time is it",
        )
        system = msgs[0]["content"]
        assert "kiso skill install" not in system
        assert "PROTECTION" not in system
        assert "Plugin installation (MANDATORY)" not in system

    async def test_skill_keyword_injects_kiso_commands(self, db):
        """Message mentioning 'skill' should inject kiso-commands appendix."""
        msgs, _ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "install the search skill",
        )
        system = msgs[0]["content"]
        assert "kiso skill install" in system

    async def test_user_keyword_injects_user_mgmt(self, db):
        """Message mentioning 'user' should inject user-mgmt appendix."""
        msgs, _ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "add a new user bob",
        )
        system = msgs[0]["content"]
        assert "PROTECTION" in system

    async def test_install_keyword_injects_plugin_install(self, db):
        """Message mentioning 'install' should inject plugin-install appendix."""
        msgs, _ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "install the browser connector",
        )
        system = msgs[0]["content"]
        assert "Plugin installation (MANDATORY)" in system

    async def test_base_prompt_always_present(self, db):
        """Core planner rules are always present regardless of message."""
        msgs, _ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "hello",
        )
        system = msgs[0]["content"]
        assert "Kiso planner" in system
        assert "CRITICAL" in system


class TestM48CuratorCategoryField:
    """48d: curator category field — prompt, schema, and validation."""

    def test_48d_curator_prompt_mentions_category(self):
        """48d: curator prompt must instruct model to include category for promote."""
        prompt = (_ROLES_DIR / "curator.md").read_text()
        assert "category" in prompt

    def test_48d_curator_prompt_lists_all_valid_categories(self):
        """48d: curator prompt must list all valid category values."""
        prompt = (_ROLES_DIR / "curator.md").read_text()
        for cat in ("project", "user", "tool", "general"):
            assert cat in prompt

    def test_48d_curator_schema_has_category_field(self):
        """48d: CURATOR_SCHEMA item must include 'category' property."""
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        assert "category" in item_props

    def test_48d_curator_schema_category_is_nullable(self):
        """48d: category field must be nullable (anyOf [string|null])."""
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        cat = item_props["category"]
        types_in_anyof = [x.get("type") for x in cat.get("anyOf", [])]
        assert "null" in types_in_anyof

    def test_48d_curator_schema_category_enum_contains_all_values(self):
        """48d: category enum must contain project/user/tool/general."""
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        cat = item_props["category"]
        enum_values = [x.get("enum", []) for x in cat.get("anyOf", []) if x.get("type") == "string"]
        flat = [v for sub in enum_values for v in sub]
        for v in ("project", "user", "tool", "general"):
            assert v in flat

    def test_48d_curator_schema_category_in_required_list(self):
        """48d: category must be in the required field list of the item schema."""
        required = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["required"]
        assert "category" in required

    def test_48d_validate_curator_accepts_valid_category(self):
        """48d: validate_curator passes when promote has a valid category."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": "project", "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_48d_validate_curator_accepts_null_category(self):
        """48d: validate_curator passes when category is null (defaults to general at runtime)."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": None, "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_48d_validate_curator_rejects_invalid_category(self):
        """48d: validate_curator rejects unknown category string values."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": "invalid_cat", "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result)
        assert any("category" in e for e in errors)

    def test_48d_validate_curator_ignores_category_for_ask(self):
        """48d: validate_curator does not enforce category for ask verdicts."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "ask", "fact": None, "category": None, "question": "Which DB?", "reason": "Unclear"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_48d_validate_curator_ignores_category_for_discard(self):
        """48d: validate_curator does not enforce category for discard verdicts."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "discard", "fact": None, "category": None, "question": None, "reason": "Transient"},
        ]}
        errors = validate_curator(result)
        assert errors == []


class TestM48SummarizerFactsTiebreaker:
    """48e: summarizer-facts tiebreaker rule for contradictions."""

    def test_48e_tiebreaker_rule_present(self):
        """48e: summarizer-facts prompt must mention contradiction resolution."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "contradict" in prompt.lower()

    def test_48e_tiebreaker_higher_confidence_wins(self):
        """48e: tiebreaker must prefer the higher-confidence fact."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "higher confidence" in prompt.lower()

    def test_48e_tiebreaker_specific_over_general(self):
        """48e: equal-confidence tiebreaker must prefer more specific fact."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "specific" in prompt.lower()


# --- M106: exit code notes, default model, prompt rules ---


class TestM106ExitCodeNotes:
    """M106c: _EXIT_CODE_NOTES dictionary completeness."""

    def test_notes_cover_expected_codes(self):
        from kiso.brain import _EXIT_CODE_NOTES
        assert 1 in _EXIT_CODE_NOTES
        assert 2 in _EXIT_CODE_NOTES
        assert 126 in _EXIT_CODE_NOTES
        assert 127 in _EXIT_CODE_NOTES
        assert -1 in _EXIT_CODE_NOTES

    def test_note_1_mentions_grep(self):
        from kiso.brain import _EXIT_CODE_NOTES
        assert "grep" in _EXIT_CODE_NOTES[1].lower()

    def test_note_127_mentions_not_found(self):
        from kiso.brain import _EXIT_CODE_NOTES
        assert "not found" in _EXIT_CODE_NOTES[127].lower()


class TestM106DefaultPlannerModel:
    """M110c: default planner model is deepseek-v3.2."""

    def test_default_planner_is_deepseek_v3_2(self):
        from kiso.config import MODEL_DEFAULTS
        assert MODEL_DEFAULTS["planner"] == "deepseek/deepseek-v3.2"


class TestM106PlannerKisoNativeFirst:
    """M106a: planner prompt must include Kiso-native-first rule."""

    def test_kiso_native_first_rule_present(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "kiso-native" in prompt.lower() or "kiso layer" in prompt.lower()

    def test_check_skills_before_os(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "apt-get" in prompt or "os-level" in prompt.lower()


class TestM106ReviewerExitCodeRules:
    """M106b: reviewer prompt covers verification task exit code semantics."""

    def test_verification_task_exit1_rule(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "nothing found" in prompt.lower() or "no matches" in prompt.lower()

    def test_anti_loop_rule(self):
        """Reviewer prompt must have anti-loop guidance."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "same output" in prompt.lower() or "retry" in prompt.lower()


class TestM106WorkerRobustCommands:
    """M106d: worker prompt includes robust command rules."""

    def test_no_find_root_rule(self):
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "find /" in prompt

    def test_command_v_recommended(self):
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "command -v" in prompt


class TestM48WorkerNoSudo:
    """48f: worker must not add sudo unless explicitly mentioned."""

    def test_48f_no_sudo_rule_present(self):
        """48f: worker prompt must have a rule about sudo usage."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "sudo" in prompt.lower()

    def test_48f_sudo_requires_explicit_mention(self):
        """48f: sudo must require explicit mention in task detail or system environment."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "explicit" in prompt.lower() or "explicitly" in prompt.lower()

    def test_48f_no_sudo_rule_says_do_not_add(self):
        """48f: rule must say not to add sudo unprompted."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "not add" in prompt.lower() or "do not add" in prompt.lower() or "never add" in prompt.lower()


# --- M89c: _MAX_MESSENGER_FACTS constant ---


class TestMessengerFactsConstant:
    def test_max_messenger_facts_value(self):
        """M89c: _MAX_MESSENGER_FACTS must equal 50 (messenger context cap)."""
        from kiso.brain import _MAX_MESSENGER_FACTS
        assert _MAX_MESSENGER_FACTS == 50


# --- M92b: _group_facts_by_category ---


class TestGroupFactsByCategory:
    """Unit tests for the extracted _group_facts_by_category helper (M92b)."""

    def _fact(self, content, category="general", session=None):
        return {"content": content, "category": category, "session": session}

    def test_empty_returns_empty_list(self):
        from kiso.brain import _group_facts_by_category
        assert _group_facts_by_category([]) == []

    def test_all_four_categories_grouped(self):
        from kiso.brain import _group_facts_by_category
        facts = [
            self._fact("proj note", "project"),
            self._fact("user pref", "user"),
            self._fact("tool info", "tool"),
            self._fact("general note", "general"),
        ]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 4
        assert any("Project" in p for p in parts)
        assert any("User" in p for p in parts)
        assert any("Tool" in p for p in parts)
        assert any("General" in p for p in parts)

    def test_unknown_category_falls_to_general(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("unknown cat fact", category="obscure")]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 1
        assert "General" in parts[0]
        assert "unknown cat fact" in parts[0]

    def test_label_session_appends_session_tag(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("fact with session", "project", session="sess-abc")]
        parts = _group_facts_by_category(facts, label_session=True)
        assert "[session:sess-abc]" in parts[0]

    def test_label_session_false_no_tag(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("fact", "project", session="sess-abc")]
        parts = _group_facts_by_category(facts, label_session=False)
        assert "[session:" not in parts[0]

    def test_facts_without_session_not_labelled(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("global fact", "project")]
        parts = _group_facts_by_category(facts, label_session=True)
        assert "[session:" not in parts[0]

    def test_empty_categories_absent_from_output(self):
        """Categories with no facts produce no section in the output."""
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("only proj", "project")]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 1
        assert "Project" in parts[0]

    def test_fact_order_within_category_preserved(self):
        """Facts within a category appear in insertion order."""
        from kiso.brain import _group_facts_by_category
        facts = [
            self._fact("first", "user"),
            self._fact("second", "user"),
            self._fact("third", "user"),
        ]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 1
        text = parts[0]
        assert text.index("first") < text.index("second") < text.index("third")

    def test_long_fact_truncated_at_200_chars(self):
        """Facts longer than _FACT_CHAR_LIMIT are truncated with ellipsis."""
        from kiso.brain import _group_facts_by_category, _FACT_CHAR_LIMIT
        long_content = "x" * (_FACT_CHAR_LIMIT + 50)
        facts = [self._fact(long_content, "project")]
        parts = _group_facts_by_category(facts)
        text = parts[0]
        # Should contain the truncated version, not the full string
        assert long_content not in text
        assert "x" * _FACT_CHAR_LIMIT in text
        assert "…" in text

    def test_short_fact_not_truncated(self):
        """Facts within _FACT_CHAR_LIMIT are kept intact."""
        from kiso.brain import _group_facts_by_category, _FACT_CHAR_LIMIT
        short_content = "y" * _FACT_CHAR_LIMIT
        facts = [self._fact(short_content, "project")]
        parts = _group_facts_by_category(facts)
        text = parts[0]
        assert short_content in text
        assert "…" not in text

    def test_fact_exactly_at_limit_not_truncated(self):
        """Facts exactly at _FACT_CHAR_LIMIT are not truncated."""
        from kiso.brain import _group_facts_by_category, _FACT_CHAR_LIMIT
        exact_content = "z" * _FACT_CHAR_LIMIT
        facts = [self._fact(exact_content, "project")]
        parts = _group_facts_by_category(facts)
        text = parts[0]
        assert exact_content in text
        assert "…" not in text


# --- M105a: _is_plugin_discovery_search ---


class TestIsPluginDiscoverySearch:
    """Unit tests for _is_plugin_discovery_search helper."""

    @pytest.mark.parametrize("detail", [
        "find browser skill in kiso registry",
        "cercare skill nel registro kiso",
        "search connector install",
        "kiso plugin discovery",
        "skill registry browse",
        "discover connector in registry",
        "search for available plugins in the registry",
        "find skill to install from kiso",
    ])
    def test_positive_matches(self, detail):
        assert _is_plugin_discovery_search(detail) is True

    @pytest.mark.parametrize("detail", [
        "latest python release",
        "browser automation tutorial",
        "how to install docker",
        "skill development best practices",
        "what is the weather today",
    ])
    def test_negative_matches(self, detail):
        assert _is_plugin_discovery_search(detail) is False


# --- M105a: validate_plan search-for-plugins ---


class TestValidatePlanPluginDiscovery:
    """M105a: search tasks for plugin discovery must be rejected."""

    def test_search_plugin_discovery_rejected(self):
        plan = {"tasks": [
            {"type": "search", "detail": "find browser skill in kiso registry",
             "expect": "skill info", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search cannot be used for kiso plugin discovery" in e for e in errors)

    def test_search_general_web_accepted(self):
        plan = {"tasks": [
            {"type": "search", "detail": "latest python release",
             "expect": "version info", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("plugin discovery" in e for e in errors)

    def test_search_plugin_install_rejected(self):
        plan = {"tasks": [
            {"type": "search", "detail": "cercare skill browser nel registro",
             "expect": "info", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search cannot be used for kiso plugin discovery" in e for e in errors)


# --- M105b: exec translator passes max_tokens ---


class TestExecTranslatorMaxTokens:
    """M105b: run_exec_translator passes max_tokens=500 to call_llm."""

    @pytest.mark.asyncio
    async def test_exec_translator_passes_max_tokens(self):
        config = _make_brain_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="echo hi") as mock_llm:
            await run_exec_translator(config, "print hi", "Linux x86_64", session="s1")
            mock_llm.assert_called_once()
            _, kwargs = mock_llm.call_args
            assert kwargs.get("max_tokens") == 500


# --- M105c: _repair_json ---


class TestRepairJson:
    """M105c: JSON repair — trailing commas + fences."""

    def test_trailing_comma_object(self):
        assert _repair_json('{"a": 1,}') == '{"a": 1}'

    def test_trailing_comma_array(self):
        assert _repair_json('[1, 2,]') == '[1, 2]'

    def test_nested_trailing_commas(self):
        repaired = _repair_json('{"a": [1,], "b": 2,}')
        parsed = json.loads(repaired)
        assert parsed == {"a": [1], "b": 2}

    def test_fences_and_comma(self):
        raw = '```json\n{"a": 1,}\n```'
        assert _repair_json(raw) == '{"a": 1}'

    def test_clean_passthrough(self):
        clean = '{"a": 1, "b": [2, 3]}'
        assert _repair_json(clean) == clean

    def test_whitespace_before_bracket(self):
        repaired = _repair_json('{"a": 1 ,  }')
        parsed = json.loads(repaired)
        assert parsed == {"a": 1}


# --- M105c: retry JSON error includes position ---


class TestRetryJsonErrorPosition:
    """M105c: retry feedback includes line/col info from JSONDecodeError."""

    @pytest.mark.asyncio
    async def test_retry_json_error_includes_position(self):
        config = _make_brain_config()
        valid_plan = json.dumps({
            "goal": "test", "secrets": None, "extend_replan": None,
            "tasks": [{"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None}],
        })
        mock_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ]
        with patch("kiso.brain.build_planner_messages", new_callable=AsyncMock,
                    return_value=(mock_messages, [])):
            with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                        side_effect=["{invalid json!!!", valid_plan]) as mock_llm:
                plan = await run_planner(
                    db=AsyncMock(), config=config, session="s1",
                    user_role="admin", new_message="test",
                )
                assert plan["goal"] == "test"
                # Check that the retry feedback message contains "line" and "col"
                retry_msg = mock_llm.call_args_list[1][0][2]  # messages arg of second call
                feedback = retry_msg[-1]["content"]  # last message = error feedback
                assert "line" in feedback.lower()
                assert "col" in feedback.lower()


# --- M105d: messenger recent messages (chat context) ---


class TestBuildMessengerMessagesRecent:
    """M105d: build_messenger_messages includes recent messages when provided."""

    def test_recent_messages_included_in_context(self):
        config = _make_brain_config()
        recent = [
            {"role": "user", "user": "alice", "content": "Is browser installed?"},
            {"role": "assistant", "content": "Yes, browser skill is installed."},
        ]
        msgs = build_messenger_messages(
            config, "", [], "follow up question",
            recent_messages=recent,
        )
        user_content = msgs[1]["content"]
        assert "Recent Conversation" in user_content
        assert "Is browser installed?" in user_content
        assert "browser skill is installed" in user_content

    def test_no_recent_messages_no_section(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi")
        user_content = msgs[1]["content"]
        assert "Recent Conversation" not in user_content

    def test_recent_messages_none_no_section(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "", [], "say hi", recent_messages=None,
        )
        user_content = msgs[1]["content"]
        assert "Recent Conversation" not in user_content

    def test_recent_messages_fenced(self):
        """Recent messages are wrapped in security fences."""
        config = _make_brain_config()
        recent = [{"role": "user", "user": "bob", "content": "hello"}]
        msgs = build_messenger_messages(
            config, "", [], "reply", recent_messages=recent,
        )
        user_content = msgs[1]["content"]
        assert "<<<MESSAGES_" in user_content
        assert "<<<END_MESSAGES_" in user_content


class TestRunMessengerIncludeRecent:
    """M105d: run_messenger loads recent messages when include_recent=True."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        await save_message(conn, "sess1", None, "user", "Is browser installed?")
        await save_message(conn, "sess1", None, "assistant", "Yes it is.")
        yield conn
        await conn.close()

    @pytest.mark.asyncio
    async def test_include_recent_true_loads_messages(self, db):
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(
                db, config, "sess1", "I don't understand",
                include_recent=True,
            )
        user_content = captured_messages[1]["content"]
        assert "Recent Conversation" in user_content
        assert "Is browser installed?" in user_content

    @pytest.mark.asyncio
    async def test_include_recent_false_no_messages(self, db):
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(db, config, "sess1", "say hi")
        user_content = captured_messages[1]["content"]
        assert "Recent Conversation" not in user_content
