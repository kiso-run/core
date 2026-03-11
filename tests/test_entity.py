"""M348: Integration tests for entity lifecycle — end-to-end entity model."""

from __future__ import annotations

import json
from unittest.mock import patch, AsyncMock

import pytest

from kiso.brain import (
    build_curator_messages,
    run_curator,
    CuratorError,
    CURATOR_VERDICT_PROMOTE,
    CURATOR_VERDICT_DISCARD,
)
from kiso.store import (
    create_session,
    find_or_create_entity,
    get_all_entities,
    get_facts,
    init_db,
    save_fact,
    save_fact_tags,
    save_learning,
    search_facts_by_entity,
)
from kiso.worker import _apply_curator_result
from kiso.config import Config, Provider


def _full_models(**overrides):
    defaults = {
        "planner": "gpt-4", "worker": "gpt-4", "reviewer": "gpt-4",
        "messenger": "gpt-4", "briefer": "gpt-4", "summarizer": "gpt-4",
        "curator": "gpt-4", "classifier": "gpt-4",
    }
    defaults.update(overrides)
    return defaults


def _full_settings(**overrides):
    defaults = {
        "context_messages": "3", "summarize_threshold": "999",
        "summarize_messages_limit": "50", "knowledge_max_facts": "200",
        "max_replan_depth": "2", "max_llm_retries": "3",
        "max_validation_retries": "3", "worker_idle_timeout": "0.01",
        "classifier_timeout": "5", "llm_timeout": "30",
        "briefer_enabled": "false",
    }
    defaults.update(overrides)
    return defaults


def _config():
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=_full_models(),
        settings=_full_settings(max_validation_retries=3),
        raw={},
    )


class TestM348EntityLifecycle:
    """Full lifecycle: learnings → curator → entities → facts → dedup."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_full_lifecycle_create_and_reuse(self, db):
        """Create entity from first curator run, reuse on second run."""
        # First curator result: creates entity + fact
        lid1 = await save_learning(db, "guidance.studio uses Webflow CMS", "sess1")
        result1 = {"evaluations": [
            {"learning_id": lid1, "verdict": "promote",
             "fact": "guidance.studio uses Webflow CMS for their website",
             "question": None, "reason": "Tech choice",
             "entity_name": "guidance.studio", "entity_kind": "website"},
        ]}
        await _apply_curator_result(db, "sess1", result1)

        entities = await get_all_entities(db)
        assert len(entities) == 1
        assert entities[0]["name"] == "guidance.studio"
        facts = await get_facts(db)
        assert len(facts) == 1
        assert facts[0]["entity_id"] == entities[0]["id"]

        # Second curator result: same entity, new fact
        lid2 = await save_learning(db, "guidance.studio has contact form", "sess1")
        result2 = {"evaluations": [
            {"learning_id": lid2, "verdict": "promote",
             "fact": "guidance.studio has a contact form with CAPTCHA",
             "question": None, "reason": "Feature detail",
             "entity_name": "guidance.studio", "entity_kind": "website"},
        ]}
        await _apply_curator_result(db, "sess1", result2)

        # Entity reused, not duplicated
        entities = await get_all_entities(db)
        assert len(entities) == 1

        # Both facts linked to same entity
        entity_facts = await search_facts_by_entity(db, entities[0]["id"])
        assert len(entity_facts) == 2

    async def test_entity_normalization(self, db):
        """Different forms of entity name resolve to same entity."""
        lid1 = await save_learning(db, "www.guidance.studio uses Webflow", "sess1")
        lid2 = await save_learning(db, "https://guidance.studio has forms", "sess1")
        lid3 = await save_learning(db, "GUIDANCE.STUDIO is a business site", "sess1")

        result = {"evaluations": [
            {"learning_id": lid1, "verdict": "promote",
             "fact": "guidance.studio uses Webflow CMS",
             "question": None, "reason": "Tech",
             "entity_name": "www.guidance.studio", "entity_kind": "website"},
            {"learning_id": lid2, "verdict": "promote",
             "fact": "guidance.studio has online forms",
             "question": None, "reason": "Feature",
             "entity_name": "https://guidance.studio/", "entity_kind": "website"},
            {"learning_id": lid3, "verdict": "promote",
             "fact": "guidance.studio is a consulting website",
             "question": None, "reason": "Category",
             "entity_name": "GUIDANCE.STUDIO", "entity_kind": "website"},
        ]}
        await _apply_curator_result(db, "sess1", result)

        entities = await get_all_entities(db)
        assert len(entities) == 1
        assert entities[0]["name"] == "guidance.studio"

        entity_facts = await search_facts_by_entity(db, entities[0]["id"])
        assert len(entity_facts) == 3

    async def test_multiple_entities(self, db):
        """Multiple distinct entities created and linked correctly."""
        lid1 = await save_learning(db, "Project uses Flask framework", "sess1")
        lid2 = await save_learning(db, "Docker used for deployment", "sess1")

        result = {"evaluations": [
            {"learning_id": lid1, "verdict": "promote",
             "fact": "Project uses Flask web framework",
             "question": None, "reason": "Tech stack",
             "entity_name": "flask", "entity_kind": "tool"},
            {"learning_id": lid2, "verdict": "promote",
             "fact": "Docker used for containerized deployment",
             "question": None, "reason": "Infra",
             "entity_name": "docker", "entity_kind": "tool"},
        ]}
        await _apply_curator_result(db, "sess1", result)

        entities = await get_all_entities(db)
        assert len(entities) == 2
        names = {e["name"] for e in entities}
        assert names == {"flask", "docker"}

        for entity in entities:
            efacts = await search_facts_by_entity(db, entity["id"])
            assert len(efacts) == 1

    async def test_existing_facts_injected_in_curator_prompt(self, db):
        """Curator receives existing entity facts for dedup context."""
        eid = await find_or_create_entity(db, "guidance.studio", "website")
        await save_fact(db, "guidance.studio has a CAPTCHA form",
                        "curator", entity_id=eid)

        existing_facts = [
            {"content": "guidance.studio has a CAPTCHA form",
             "entity_name": "guidance.studio"},
        ]
        msgs = build_curator_messages(
            [{"id": 1, "content": "guidance.studio form has CAPTCHA"}],
            existing_facts=existing_facts,
        )
        user_content = msgs[1]["content"]
        assert "## Existing Facts (already in knowledge base)" in user_content
        assert "guidance.studio has a CAPTCHA form" in user_content
        assert "[entity: guidance.studio]" in user_content


class TestM348EntityMigration:
    """M348: migration from entity: tags to entity records."""

    async def test_migration_lifecycle(self, tmp_path):
        """End-to-end: old entity: tags → migrated entity records."""
        db = await init_db(tmp_path / "test.db")
        # Simulate old-style entity: tags
        fid1 = await save_fact(db, "Flask uses Jinja2 templates", "curator")
        fid2 = await save_fact(db, "Flask supports async views", "curator")
        await save_fact_tags(db, fid1, ["entity:flask", "tech-stack"])
        await save_fact_tags(db, fid2, ["entity:flask"])
        await db.close()

        # Re-init triggers migration
        db = await init_db(tmp_path / "test.db")

        # Entity created from tag
        entities = await get_all_entities(db)
        assert len(entities) == 1
        assert entities[0]["name"] == "flask"
        assert entities[0]["kind"] == "tool"  # default from migration

        # Facts linked to entity
        entity_facts = await search_facts_by_entity(db, entities[0]["id"])
        assert len(entity_facts) == 2

        # entity: tags removed, non-entity tags preserved
        import aiosqlite
        cur = await db.execute("SELECT tag FROM fact_tags WHERE tag LIKE 'entity:%'")
        assert await cur.fetchall() == []
        cur = await db.execute("SELECT tag FROM fact_tags WHERE fact_id = ?", (fid1,))
        tags = [r[0] for r in await cur.fetchall()]
        assert "tech-stack" in tags

        await db.close()
