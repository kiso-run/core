"""Integration tests for the briefer pipeline (M253).

These tests verify end-to-end briefer behavior with mocked LLM responses,
covering different request types and fallback scenarios.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from kiso.brain import (
    BRIEFER_MODULES,
    BRIEFER_SCHEMA,
    BrieferError,
    build_briefer_messages,
    build_planner_messages,
    run_briefer,
    validate_briefing,
)
from kiso.config import Config, Provider
from kiso.llm import LLMError
from kiso.store import (
    create_session,
    init_db,
    save_fact,
    save_fact_tags,
    save_message,
)
from kiso.worker.utils import ExecutionState
from tests.conftest import full_settings, full_models


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(briefer_enabled=True) -> Config:
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=full_models(),
        settings=full_settings(context_messages=3, briefer_enabled=briefer_enabled),
        raw={},
    )


def _briefing(
    modules=None, tools=None, exclude_recipes=None, context="", output_indices=None,
    relevant_tags=None, relevant_entities=None,
) -> dict:
    return {
        "modules": modules or [],
        "tools": tools or [],
        "exclude_recipes": exclude_recipes or [],
        "context": context,
        "output_indices": output_indices or [],
        "relevant_tags": relevant_tags or [],
        "relevant_entities": relevant_entities or [],
    }


@pytest.fixture()
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    await create_session(conn, "sess1")
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# Scenario tests: briefer selects appropriate context per request type
# ---------------------------------------------------------------------------


class TestBrieferScenarios:
    """Integration tests simulating different request types through the briefer."""

    async def test_simple_request_minimal_briefing(self, db):
        """Simple question → briefer selects no modules, no skills, minimal context."""
        briefing = _briefing(context="User wants to know the time.")

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "what time is it?",
            )

        system = msgs[0]["content"]
        user_content = msgs[1]["content"]
        # Core rules present, conditional modules absent
        assert "Kiso planner" in system
        assert "Web interaction:" not in system
        assert "extend_replan" not in system
        # Synthesized context used
        assert "User wants to know the time." in user_content

    async def test_web_request_selects_web_module(self, db):
        """Web request → briefer selects web module + browser skill."""
        briefing = _briefing(
            modules=["web"],
            tools=["browser"],
            context="User wants to visit gazzetta.it for sports news.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        # M387: provide browser skill so briefer selection isn't cleared
        fake_skills = [
            {"name": "browser", "summary": "Navigate, click, fill, screenshot",
             "args_schema": {}, "env": {}, "session_secrets": [],
             "path": "/fake", "version": "0.1.0", "description": ""},
        ]
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "admin", "vai su gazzetta.it",
            )

        system = msgs[0]["content"]
        user_content = msgs[1]["content"]
        # Web module injected
        assert "Web interaction:" in system
        # Browser skill present
        # build_planner_tool_list rebuilds full descriptions from installed tools
        assert "browser" in user_content
        assert "Navigate, click, fill, screenshot" in user_content
        # Other modules absent
        assert "extend_replan" not in system
        assert "Scripting:" not in system

    async def test_replan_selects_replan_module(self, db):
        """Replan context → briefer selects replan module + failure context."""
        briefing = _briefing(
            modules=["replan"],
            context="Previous plan failed: browser skill not installed.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "retry the previous plan",
            )

        system = msgs[0]["content"]
        # Replan module injected
        assert "extend_replan" in system
        assert "Strategy diversification" in system or "fundamentally different strategy" in system
        # Web module absent
        assert "Web interaction:" not in system

    async def test_messenger_briefing_filters_outputs(self):
        """Messenger briefer selects only outputs with reportable data."""
        pool = {
            "plan_outputs": (
                "[0] exec: install browser → done\n"
                "[1] exec: cleanup temp → done\n"
                "[2] search: latest news → 5 headlines found"
            ),
        }
        msgs = build_briefer_messages("messenger", "report results to user", pool)
        content = msgs[1]["content"]
        assert "messenger" in content
        assert "Plan Outputs" in content
        assert "install browser" in content  # all outputs passed to briefer
        assert "latest news" in content

    async def test_worker_briefing_includes_outputs(self):
        """Worker briefer receives plan_outputs for dependency resolution."""
        pool = {
            "plan_outputs": (
                "[0] exec: download file.csv → saved to /tmp/file.csv\n"
                "[1] exec: pip install pandas → done"
            ),
        }
        msgs = build_briefer_messages("worker", "analyze the CSV data", pool)
        content = msgs[1]["content"]
        assert "worker" in content
        assert "/tmp/file.csv" in content

    async def test_multiple_modules_combined(self, db):
        """Complex request → briefer selects multiple modules."""
        briefing = _briefing(
            modules=["web", "data_flow"],
            tools=["browser: navigate", "python: run scripts"],
            context="User wants to scrape a site and process data with Python.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "scrape the site and analyze",
            )

        system = msgs[0]["content"]
        assert "Web interaction:" in system
        assert "Download/fetch content" in system or "save to file" in system
        assert "One-liner execution" in system or "One-liners" in system
        # Replan and tool_recovery NOT included
        assert "extend_replan" not in system
        assert "Broken tool deps" not in system


# ---------------------------------------------------------------------------
# Fallback tests
# ---------------------------------------------------------------------------


class TestBrieferFallback:
    """Tests verifying graceful fallback when briefer fails."""

    async def test_llm_error_falls_back(self, db):
        """LLMError from briefer → full context used, no crash."""
        async def _failing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                raise LLMError("model unavailable")
            return json.dumps({
                "goal": "test", "secrets": None,
                "tasks": [{"type": "msg", "detail": "Answer in English. hi",
                           "tool": None, "args": None, "expect": None}],
            })

        with patch("kiso.brain.call_llm", side_effect=_failing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "hello",
            )

        system = msgs[0]["content"]
        user_content = msgs[1]["content"]
        # Full prompt (all modules included)
        assert "Kiso planner" in system
        # Standard context sections (fallback path)
        assert "## System Environment" in user_content

    async def test_invalid_json_falls_back(self, db):
        """Invalid JSON from briefer → falls back gracefully."""
        async def _bad_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return "not valid json at all"
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_bad_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "hello",
            )

        # Should not crash — fallback to full context
        assert "Kiso planner" in msgs[0]["content"]
        assert "## System Environment" in msgs[1]["content"]

    async def test_briefer_disabled_uses_full_context(self, db):
        """briefer_enabled=False → original behavior, no briefer call."""
        call_log = []

        async def _logging_llm(cfg, role, messages, **kw):
            call_log.append(role)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_logging_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(briefer_enabled=False), "sess1", "user", "hello",
            )

        # Briefer should NOT be called
        assert "briefer" not in call_log
        # Full context used
        assert "## System Environment" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# Tag-enriched briefing (briefer + tags pipeline)
# ---------------------------------------------------------------------------


class TestBrieferTagPipeline:
    """End-to-end tests for briefer tag selection + fact retrieval."""

    async def test_tags_in_context_pool_reach_briefer(self, db):
        """Tags from fact_tags table appear in briefer's context pool."""
        fid = await save_fact(db, "PostgreSQL on port 5432", "test", category="project")
        await save_fact_tags(db, fid, ["database", "postgres"])

        captured = []

        async def _capturing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured.append(messages[1]["content"])
                return json.dumps(_briefing())
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_capturing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            await build_planner_messages(
                db, _config(), "sess1", "user", "check db status",
            )

        assert captured
        assert "database" in captured[0]
        assert "postgres" in captured[0]

    async def test_tag_retrieval_adds_new_facts(self, db):
        """Briefer selects tags → additional facts appended to context."""
        # FTS-matched fact
        await save_fact(db, "Python version 3.12 deployed", "test", category="project")
        # Tag-only fact (not in FTS results for "Python")
        tag_id = await save_fact(db, "Redis cluster on port 6379", "test", category="project")
        await save_fact_tags(db, tag_id, ["infra"])

        briefing = _briefing(
            context="User asks about Python setup.",
            relevant_tags=["infra"],
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "Python version",
            )

        user_content = msgs[1]["content"]
        assert "Redis cluster on port 6379" in user_content
        assert "## Relevant Facts" in user_content

    async def test_no_tags_no_extra_section(self, db):
        """When briefer returns empty relevant_tags, no additional section."""
        briefing = _briefing(context="Simple answer.")

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "hi",
            )

        assert "## Relevant Facts" not in msgs[1]["content"]


# ---------------------------------------------------------------------------
# Briefer prompt budget test
# ---------------------------------------------------------------------------


class TestBrieferPromptBudget:
    """Verify briefer prompts fit in reasonable token budgets."""

    def test_briefer_system_prompt_is_compact(self):
        """Briefer system prompt should be under 600 words."""
        msgs = build_briefer_messages("planner", "test", {})
        system = msgs[0]["content"]
        word_count = len(system.split())
        assert word_count < 600, f"Briefer system prompt is {word_count} words (max 600)"

    def test_briefer_with_full_pool_under_budget(self):
        """Even with a full context pool, briefer input stays reasonable."""
        pool = {
            "summary": "Session about building a web scraper",
            "facts": "\n".join(f"- Fact {i}: some project detail" for i in range(20)),
            "recent_messages": "\n".join(
                f"[user] marco: message {i}" for i in range(5)
            ),
            "tools": "\n".join(f"tool_{i}: does thing {i}" for i in range(10)),
            "pending": "- Question about API key\n- Question about deployment",
            "plan_outputs": "\n".join(
                f"[{i}] exec: task {i} → output {i}" for i in range(5)
            ),
            "available_tags": ", ".join(f"tag-{i}" for i in range(30)),
        }
        msgs = build_briefer_messages("planner", "do the next step", pool)
        total_chars = sum(len(m["content"]) for m in msgs)
        # Should be well under 10K chars (~2500 tokens) for the briefer input
        assert total_chars < 10000, f"Briefer input is {total_chars} chars (max 10000)"

    def test_schema_required_fields_match_validate(self):
        """BRIEFER_SCHEMA required fields match what validate_briefing checks."""
        schema_required = set(
            BRIEFER_SCHEMA["json_schema"]["schema"]["required"]
        )
        # validate_briefing checks each of these
        expected = {"modules", "tools", "exclude_recipes", "context", "output_indices", "relevant_tags", "relevant_entities"}
        assert schema_required == expected


# ---------------------------------------------------------------------------
# M450: MD skills in briefer context pool
# ---------------------------------------------------------------------------


class TestRecipesInBriefer:
    """Verify recipes are injected into the briefer context pool."""

    async def test_recipes_appear_in_context_pool(self, db):
        """When recipes exist, they appear in the briefer context pool."""
        fake_recipes = [
            {"name": "data-analyst", "summary": "Data analysis guidance",
             "instructions": "Use pandas for tabular data.", "path": "/fake/data-analyst.md"},
        ]
        briefing = _briefing(context="User wants data analysis help.")

        captured_messages = []

        async def _capturing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_capturing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_recipes", return_value=fake_recipes):
            await build_planner_messages(
                db, _config(), "sess1", "user", "analyze this data",
            )

        # Briefer should receive the recipes section
        briefer_input = captured_messages[1]["content"]
        assert "Available Recipes" in briefer_input
        assert "data-analyst" in briefer_input
        assert "Data analysis guidance" in briefer_input

    async def test_no_recipes_no_section(self, db):
        """When no recipes exist, no recipes section in briefer context."""
        briefing = _briefing(context="Simple request.")

        captured_messages = []

        async def _capturing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_capturing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_recipes", return_value=[]):
            await build_planner_messages(
                db, _config(), "sess1", "user", "hello",
            )

        briefer_input = captured_messages[1]["content"]
        assert "Available Recipes" not in briefer_input

    async def test_recipes_in_planner_context_when_briefer_selects(self, db):
        """Recipe instructions appear in planner messages when briefer passes them through."""
        fake_recipes = [
            {"name": "data-analyst", "summary": "Data analysis guidance",
             "instructions": "Use pandas for tabular data.", "path": "/fake/data-analyst.md"},
        ]
        briefing = _briefing(
            context="User wants data analysis help.\n\n"
                    "## Available Recipes\n- data-analyst — Data analysis guidance\n  Use pandas for tabular data.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_recipes", return_value=fake_recipes):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "analyze this data",
            )

        user_content = msgs[1]["content"]
        assert "pandas" in user_content


# ---------------------------------------------------------------------------
# M824 — Tool injection: skip briefer filtering when few tools installed
# ---------------------------------------------------------------------------


class TestM824ToolFilterThreshold:
    """M824: briefer tool filter is skipped when installed tools <= threshold."""

    async def test_few_tools_injects_all(self, db):
        """5 tools installed with threshold=10 → planner sees all 5."""
        # Briefer selects only 'browser' — but M824 overrides
        briefing = _briefing(
            modules=["web"],
            tools=["browser"],
            context="User wants to read a screenshot.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        fake_tools = [
            {"name": n, "summary": f"{n} tool", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake",
             "version": "0.1.0", "description": ""}
            for n in ["browser", "ocr", "aider", "docreader", "transcriber"]
        ]

        cfg = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=full_models(),
            settings=full_settings(
                briefer_enabled=True,
                briefer_tool_filter_threshold=10,
            ),
            raw={},
        )

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=fake_tools):
            msgs, _, _ = await build_planner_messages(
                db, cfg, "sess1", "admin", "read the screenshot",
            )

        user_content = msgs[1]["content"]
        # All 5 tools should be present, not just briefer's selection
        for name in ["browser", "ocr", "aider", "docreader", "transcriber"]:
            assert name in user_content, f"Tool '{name}' missing from planner context"

    async def test_many_tools_uses_briefer_filter(self, db):
        """15 tools with threshold=10 → only briefer-selected tools appear."""
        briefing = _briefing(
            modules=["web"],
            tools=["browser"],
            context="User wants to browse.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        fake_tools = [
            {"name": f"tool{i}", "summary": f"tool{i} desc", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake",
             "version": "0.1.0", "description": ""}
            for i in range(15)
        ]
        # Add browser so briefer selection works
        fake_tools.append(
            {"name": "browser", "summary": "Navigate pages", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake",
             "version": "0.1.0", "description": ""}
        )

        cfg = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=full_models(),
            settings=full_settings(
                briefer_enabled=True,
                briefer_tool_filter_threshold=10,
            ),
            raw={},
        )

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=fake_tools):
            msgs, _, _ = await build_planner_messages(
                db, cfg, "sess1", "admin", "browse example.com",
            )

        user_content = msgs[1]["content"]
        # Only browser selected by briefer (16 tools > threshold 10)
        assert "browser" in user_content
        assert "Navigate pages" in user_content
        # Other tools should NOT be present
        assert "tool0 desc" not in user_content

    async def test_threshold_zero_always_filters(self, db):
        """threshold=0 → briefer filtering applies even with 1 tool."""
        # Briefer selects only 'browser' out of 2 tools
        briefing = _briefing(
            modules=["web"],
            tools=["browser"],
            context="User wants to browse.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        fake_tools = [
            {"name": "browser", "summary": "Navigate pages", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake",
             "version": "0.1.0", "description": ""},
            {"name": "ocr", "summary": "Extract text from images", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake",
             "version": "0.1.0", "description": ""},
        ]

        cfg = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=full_models(),
            settings=full_settings(
                briefer_enabled=True,
                briefer_tool_filter_threshold=0,
            ),
            raw={},
        )

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=fake_tools):
            msgs, _, _ = await build_planner_messages(
                db, cfg, "sess1", "admin", "browse example.com",
            )

        user_content = msgs[1]["content"]
        # threshold=0 → briefer filtering applies: only browser, not ocr
        assert "Navigate pages" in user_content
        assert "Extract text from images" not in user_content


# ---------------------------------------------------------------------------
# M825 — Planner prompt: session file awareness rules
# ---------------------------------------------------------------------------


class TestM825SessionFilesModule:
    """M825: session_files module injected when workspace has files."""

    async def test_session_with_files_gets_module(self, db):
        """Session workspace files → session_files module in prompt."""
        briefing = _briefing(
            modules=["planning_rules"],
            context="User wants to read a file.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch(
                 "kiso.worker.utils._build_execution_state",
                 return_value=ExecutionState(
                     session="sess1",
                     workspace_root="/tmp/sess1",
                     workspace_files=[{
                         "path": "test.png",
                         "abs_path": "/tmp/sess1/test.png",
                         "size": 1024,
                         "size_human": "1 KB",
                         "type": "image",
                         "age_human": "just now",
                     }],
                 ),
             ):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "admin", "read the screenshot",
            )

        system = msgs[0]["content"]
        assert "Session file rules:" in system
        assert "exact path shown in the Session Workspace listing" in system
        # M933: verify the file listing also appears in user content
        content = msgs[1]["content"]
        assert "## Session Workspace" in content
        assert "test.png" in content

    async def test_empty_session_no_module(self, db):
        """Empty session → session_files module absent."""
        briefing = _briefing(
            modules=["planning_rules"],
            context="Simple chat.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch(
                 "kiso.worker.utils._build_execution_state",
                 return_value=ExecutionState(
                     session="sess1",
                     workspace_root="/tmp/sess1",
                     workspace_files=[],
                 ),
             ):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "admin", "hello",
            )

        system = msgs[0]["content"]
        assert "Session file rules:" not in system
