"""Integration tests for P51-P57 fixes — messenger quality & entity backfill."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiso.brain import run_briefer, validate_plan
from kiso.config import MODEL_DEFAULTS, REASONING_DEFAULTS, Config, Provider
from kiso.store import (
    backfill_fact_entities,
    create_session,
    find_or_create_entity,
    init_db,
    save_fact,
    save_fact_tags,
    search_facts_by_entity,
)
from kiso.worker.loop import _msg_task
from tests.conftest import full_settings, full_models


def _config(**settings_overrides):
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=full_models(),
        settings=full_settings(briefer_enabled=True, bot_name="Kiso", **settings_overrides),
        raw={},
    )


# ---------------------------------------------------------------------------
# P51: Boot fact entity backfill
# ---------------------------------------------------------------------------


class TestBootFactEntityBackfill:
    """P51: facts with NULL entity_id become visible after backfill."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_orphan_fact_becomes_searchable_after_backfill(self, db):
        """Fact created before entity model → backfill → entity query finds it."""
        # Simulate pre-entity fact (no entity_id)
        fid = await save_fact(db, "Instance SSH public key: self ssh-ed25519 AAAA",
                              "system", session=None, category="system")
        # Create entity later (as boot does)
        eid = await find_or_create_entity(db, "self", "system")

        # Before backfill: entity query returns nothing
        results = await search_facts_by_entity(db, eid)
        assert len(results) == 0

        # After backfill: fact is linked
        updated = await backfill_fact_entities(db)
        assert updated == 1

        results = await search_facts_by_entity(db, eid)
        assert len(results) == 1
        assert results[0]["id"] == fid

    async def test_backfill_idempotent(self, db):
        """Running backfill twice doesn't double-link."""
        await save_fact(db, "Instance self runs on host foo", "system")
        await find_or_create_entity(db, "self", "system")

        first = await backfill_fact_entities(db)
        second = await backfill_fact_entities(db)
        assert first == 1
        assert second == 0  # already linked


# ---------------------------------------------------------------------------
# P52/P56: Messenger model config
# ---------------------------------------------------------------------------


class TestMessengerModelConfig:
    """P52/P56: messenger uses deepseek-v3.2 (not qwen)."""

    def test_messenger_model_is_deepseek(self):
        assert MODEL_DEFAULTS["messenger"] == "deepseek/deepseek-v3.2"

    def test_messenger_not_in_reasoning_defaults(self):
        assert "messenger" not in REASONING_DEFAULTS


# ---------------------------------------------------------------------------
# P53: Tags in messenger briefer context pool
# ---------------------------------------------------------------------------


class TestMessengerBrieferTagInjection:
    """P53: _msg_task injects available_tags into briefer context pool."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_tags_reach_briefer(self, db):
        """When tags exist, briefer receives them in context pool."""
        fid = await save_fact(db, "Flask uses Jinja2 templates", "curator")
        await save_fact_tags(db, fid, ["flask", "web-framework"])

        briefer_msgs = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                briefer_msgs.append(messages)
                return json.dumps({
                    "modules": [], "wrappers": [], "context": "",
                    "output_indices": [], "relevant_tags": [],
                    "exclude_recipes": [], "relevant_entities": [], "mcp_methods": [],
                })
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm):
            await _msg_task(_config(), db, "sess1", "Tell me about flask")

        content = briefer_msgs[0][1]["content"]
        assert "flask" in content
        assert "web-framework" in content


# ---------------------------------------------------------------------------
# P54: Msg detail validation
# ---------------------------------------------------------------------------


class TestMsgDetailValidation:
    """P54: validate_plan rejects empty msg detail after language prefix."""

    def test_only_language_prefix_rejected(self):
        """msg detail with only language prefix is rejected (too short)."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in Italian.",
             "expect": None, "wrapper": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("empty or too short" in e for e in errors)

    def test_substantive_detail_accepted(self):
        plan = {"tasks": [
            {"type": "msg",
             "detail": "Answer in Italian. Tell the user the SSH key is at ~/.kiso/sys/ssh/",
             "expect": None, "wrapper": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("empty or too short" in e for e in errors)

    def test_m902_no_prefix_substantive_detail_accepted(self):
        """msg detail without prefix is accepted if substantive (_msg_task adds prefix)."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Tell the user the results",
             "expect": None, "wrapper": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("empty or too short" in e for e in errors)
        assert not any("must start with" in e for e in errors)


# ---------------------------------------------------------------------------
# P55: Briefer wrapper filter with no wrappers installed
# ---------------------------------------------------------------------------


class TestBrieferToolFilterNoTools:
    """P55: clears hallucinated wrappers when none installed."""

    @pytest.fixture()
    def config(self):
        return _config()

    @pytest.mark.asyncio
    async def test_no_tools_clears_all(self, config):
        """When context_pool has no wrappers, briefer wrappers are cleared."""
        response = json.dumps({
            "modules": [],
            "wrappers": ["browser", "aider"],
            "context": "Some context",
            "output_indices": [],
            "relevant_tags": [],
            "exclude_recipes": [], "relevant_entities": [], "mcp_methods": [],
        })
        with patch("kiso.brain.call_llm", return_value=response):
            result = await run_briefer(config, "planner", "test", {})
        assert result["wrappers"] == []

    @pytest.mark.asyncio
    async def test_with_tools_filters_correctly(self, config):
        """When context_pool has wrappers, only matching ones pass."""
        response = json.dumps({
            "modules": [],
            "wrappers": ["browser", "fake_skill"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "exclude_recipes": [], "relevant_entities": [], "mcp_methods": [],
        })
        ctx = {"wrappers": "Available wrappers:\n- browser — navigate, click, fill, screenshot"}
        with patch("kiso.brain.call_llm", return_value=response):
            result = await run_briefer(config, "planner", "test", ctx)
        assert "browser" in result["wrappers"]
        assert "fake_skill" not in result["wrappers"]

    @pytest.mark.asyncio
    async def test_tool_filter_exact_name_no_substring(self, config):
        """'git' installed must NOT match briefer wrapper 'github'."""
        response = json.dumps({
            "modules": [],
            "wrappers": ["github", "git"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "exclude_recipes": [], "relevant_entities": [], "mcp_methods": [],
        })
        ctx = {"wrappers": "Available wrappers:\n- git — version control operations"}
        with patch("kiso.brain.call_llm", return_value=response):
            result = await run_briefer(config, "planner", "test", ctx)
        assert "git" in result["wrappers"]
        assert "github" not in result["wrappers"]
