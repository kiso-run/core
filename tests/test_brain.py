"""Tests for kiso/brain.py — planner brain."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import aiosqlite
from kiso.brain import (
    ClassifierError,
    CuratorError,
    ExecTranslatorError,
    MessengerError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    _load_system_prompt,
    _ROLES_DIR,
    _strip_fences,
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
        assert "task planner" in prompt

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

VALID_REVIEW_OK = json.dumps({"status": "ok", "reason": None, "learn": None, "retry_hint": None})
VALID_REVIEW_REPLAN = json.dumps({"status": "replan", "reason": "File missing", "learn": None, "retry_hint": None})
VALID_REVIEW_WITH_LEARN = json.dumps({"status": "ok", "reason": None, "learn": "Uses Python 3.12", "retry_hint": None})
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
        """The default planner prompt should mention Reference docs."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Reference docs" in prompt
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
        """M40: pub/ filesystem path vs /pub/ HTTP URL must be explicitly distinguished."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "HTTP download URL" in prompt
        assert "filesystem path" in prompt

    def test_m45_plugin_install_uses_registry_not_search(self):
        """M45: planner prompt must forbid web search for kiso plugin discovery."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "NEVER use" in prompt or "NEVER" in prompt
        assert "registry" in prompt.lower()
        assert "web search" in prompt.lower()

    def test_m45_plugin_install_rule_is_mandatory(self):
        """M45: plugin installation rule must be marked MANDATORY."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Plugin installation (MANDATORY)" in prompt


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
        from kiso.brain import REVIEW_SCHEMA
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
