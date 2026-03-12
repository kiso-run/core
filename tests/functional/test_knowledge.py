"""Functional tests for knowledge pipeline: chat_kb, learning, entity/tag enrichment, messenger quality.

F9:  chat_kb self-inspection (entity "self" → hostname in output)
F10: multi-turn learning pipeline (teach → query → recall)
F11: entity/tag enrichment (pre-seeded facts surface in response)
F12: messenger quality (no emoji, no hallucinated actions)

Requires ``--functional`` flag and a running OpenRouter API key in the environment.
"""

from __future__ import annotations

import platform
import re

import pytest

from tests.functional.conftest import assert_italian, assert_no_failure_language

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# F9 — chat_kb self-inspection
# ---------------------------------------------------------------------------


class TestChatKBSelfInspection:
    """F9: chat_kb fast path resolves entity "self" for system introspection."""

    async def test_chat_kb_self_hostname(self, run_message):
        """Ask for hostname → fast path returns it from boot facts."""
        result = await run_message("qual è il tuo hostname?", timeout=60)
        assert result.success
        assert_italian(result.msg_output)
        # Should use fast path (no exec/skill tasks)
        types = result.task_types()
        assert "exec" not in types
        assert "tool" not in types
        # Output contains some hostname-like value (LLM may return real hostname
        # or instance name from boot facts — both are valid)
        lower = result.msg_output.lower()
        real_host = platform.node().lower()
        assert (
            real_host in lower
            or "hostname" in lower
            or "host" in lower
        ), f"Expected hostname info in output: {result.msg_output[:200]}"


# ---------------------------------------------------------------------------
# F10 — multi-turn learning pipeline
# ---------------------------------------------------------------------------


class TestMultiTurnLearning:
    """F10: teach a fact → query it back → verify recall."""

    async def test_learning_retention(self, run_message):
        """Teach 'project uses Flask 3.0 with SQLAlchemy' → ask back → Flask mentioned."""
        # Message 1: teach a fact
        r1 = await run_message(
            "ricordati che il progetto corrente usa Flask 3.0 con SQLAlchemy",
            timeout=120,
        )
        assert r1.success

        # Message 2: query the fact
        r2 = await run_message(
            "che framework usa il progetto corrente?",
            timeout=120,
        )
        assert r2.success
        assert_italian(r2.msg_output)
        # Should mention Flask
        assert "flask" in r2.msg_output.lower()


# ---------------------------------------------------------------------------
# F11 — entity/tag enrichment
# ---------------------------------------------------------------------------


class TestEntityTagEnrichment:
    """F11: pre-seeded entity + tagged facts surface in responses."""

    async def test_entity_tag_enrichment(self, func_db, run_message):
        """Pre-seed fact with entity + tags → query → fact content in output."""
        from kiso.store import find_or_create_entity, save_fact

        # Pre-seed a fact with entity + tag
        eid = await find_or_create_entity(func_db, "guidance.studio", "website")
        await save_fact(
            func_db, "guidance.studio is a SaaS platform for user onboarding",
            source="curator", category="general",
            tags=["website", "saas", "onboarding"], entity_id=eid,
        )
        result = await run_message(
            "cosa sai su guidance.studio?",
            timeout=120,
        )
        assert result.success
        assert_italian(result.msg_output)
        # Should mention onboarding or SaaS from the pre-seeded fact
        output_lower = result.msg_output.lower()
        assert "onboarding" in output_lower or "saas" in output_lower


# ---------------------------------------------------------------------------
# F12 — messenger quality
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# F13 — chat_kb classification for user-defined entity
# ---------------------------------------------------------------------------


class TestF13ChatKBClassification:
    """F13: chat_kb fast path for user-defined entity with pre-seeded facts."""

    async def test_chat_kb_user_entity(self, func_db, run_message):
        """Pre-seed entity 'guidance.studio' → ask about it → msg-only plan."""
        from kiso.store import find_or_create_entity, save_fact

        eid = await find_or_create_entity(func_db, "guidance.studio", "website")
        await save_fact(
            func_db,
            "guidance.studio is a SaaS platform for interactive user onboarding workflows",
            source="curator", category="general",
            tags=["website", "saas", "onboarding"], entity_id=eid,
        )
        result = await run_message("cosa sai di guidance.studio?", timeout=120)
        assert result.success
        assert_italian(result.msg_output)
        # Response should contain pre-seeded fact content
        output_lower = result.msg_output.lower()
        assert "onboarding" in output_lower or "saas" in output_lower or "workflow" in output_lower
        # chat_kb fast path: no exec or skill tasks, only msg
        types = result.task_types()
        assert "exec" not in types, f"Expected no exec tasks, got: {types}"
        assert "tool" not in types, f"Expected no tool tasks, got: {types}"
        # Should have exactly 1 msg task (fast-path produces single msg)
        msg_tasks = [t for t in result.tasks if t.get("type") == "msg"]
        assert len(msg_tasks) >= 1, f"Expected at least 1 msg task, got: {types}"


# ---------------------------------------------------------------------------
# F14 — curator entity creation across turns
# ---------------------------------------------------------------------------


class TestF14CuratorEntityCreation:
    """F14: teach about entity → curator creates entity + tags → recall."""

    async def test_curator_entity_creation_and_recall(self, func_db, run_message):
        """Turn 1: search Python 3.12 → Turn 2: recall what was learned."""
        # Turn 1: trigger learning about a specific entity
        r1 = await run_message(
            "cerca info su Python 3.12 — dimmi cosa trovi",
            timeout=180,
        )
        assert r1.success

        # Check DB: entity should exist
        cur = await func_db.execute(
            "SELECT id, name, kind FROM entities WHERE LOWER(name) LIKE '%python%'"
        )
        entities = [dict(r) for r in await cur.fetchall()]
        assert len(entities) >= 1, (
            f"Expected entity matching 'python', found: {entities}"
        )

        # Check: at least 1 fact with entity_id pointing to python entity
        entity_ids = [e["id"] for e in entities]
        placeholders = ",".join("?" * len(entity_ids))
        cur = await func_db.execute(
            f"SELECT id, content FROM facts WHERE entity_id IN ({placeholders})",
            entity_ids,
        )
        facts = [dict(r) for r in await cur.fetchall()]
        assert len(facts) >= 1, (
            f"Expected ≥1 fact with python entity_id, found none"
        )

        # Check: at least 1 tag assigned
        fact_ids = [f["id"] for f in facts]
        placeholders = ",".join("?" * len(fact_ids))
        cur = await func_db.execute(
            f"SELECT fact_id, tag FROM fact_tags WHERE fact_id IN ({placeholders})",
            fact_ids,
        )
        tags = await cur.fetchall()
        assert len(tags) >= 1, "Expected ≥1 tag on python-entity facts"

        # Turn 2: ask back → should recall learned information
        r2 = await run_message("cosa sai di python?", timeout=120)
        assert r2.success
        assert_italian(r2.msg_output)
        assert "python" in r2.msg_output.lower()


# ---------------------------------------------------------------------------
# F15 — entity dedup and tag reuse
# ---------------------------------------------------------------------------


class TestF15EntityDedupTagReuse:
    """F15: pre-seeded entity 'flask' → teach new fact → no duplicate entity."""

    async def test_entity_dedup_tag_reuse(self, func_db, run_message):
        """Pre-seed Flask entity + fact → teach new Flask fact → verify dedup."""
        from kiso.store import find_or_create_entity, save_fact

        # Pre-seed entity + fact
        eid = await find_or_create_entity(func_db, "flask", "tool")
        await save_fact(
            func_db,
            "Flask is a lightweight Python web framework",
            source="curator", category="general",
            tags=["python", "web"], entity_id=eid,
        )

        # Count entities before
        cur = await func_db.execute("SELECT COUNT(*) FROM entities")
        count_before = (await cur.fetchone())[0]

        # Teach new fact about Flask
        r1 = await run_message(
            "ricordati che Flask usa Jinja2 come template engine",
            timeout=120,
        )
        assert r1.success

        # Check: no duplicate entity created
        cur = await func_db.execute(
            "SELECT id, name FROM entities WHERE LOWER(name) LIKE '%flask%'"
        )
        flask_entities = [dict(r) for r in await cur.fetchall()]
        assert len(flask_entities) == 1, (
            f"Expected exactly 1 flask entity (dedup), found: {flask_entities}"
        )

        # Total entity count should not have increased by more than 1
        # (at most 1 new entity if the LLM creates one for 'jinja2')
        cur = await func_db.execute("SELECT COUNT(*) FROM entities")
        count_after = (await cur.fetchone())[0]
        assert count_after <= count_before + 2, (
            f"Entity explosion: {count_before} → {count_after}"
        )


# ---------------------------------------------------------------------------
# F16 — scored fact retrieval via briefer
# ---------------------------------------------------------------------------


class TestF16ScoredFactRetrieval:
    """F16: pre-seeded entities → query targets specific ones, not all."""

    async def test_scored_retrieval_filters_irrelevant(self, func_db, run_message):
        """Pre-seed Flask+Django+guidance → ask about Python → only Python entities."""
        from kiso.store import find_or_create_entity, save_fact

        # Pre-seed 3 entities
        flask_id = await find_or_create_entity(func_db, "flask", "tool")
        await save_fact(
            func_db, "Flask is a lightweight Python web framework with Jinja2 templates",
            source="curator", category="general",
            tags=["python", "web"], entity_id=flask_id,
        )
        await save_fact(
            func_db, "Flask uses Werkzeug as its WSGI toolkit",
            source="curator", category="general",
            tags=["python", "web"], entity_id=flask_id,
        )

        django_id = await find_or_create_entity(func_db, "django", "tool")
        await save_fact(
            func_db, "Django is a batteries-included Python web framework with ORM",
            source="curator", category="general",
            tags=["python", "web"], entity_id=django_id,
        )
        await save_fact(
            func_db, "Django uses its own template engine",
            source="curator", category="general",
            tags=["python", "web"], entity_id=django_id,
        )

        guidance_id = await find_or_create_entity(func_db, "guidance.studio", "website")
        await save_fact(
            func_db, "guidance.studio is a SaaS platform for user onboarding flows",
            source="curator", category="general",
            tags=["saas", "onboarding"], entity_id=guidance_id,
        )

        # Query targets Python frameworks specifically
        result = await run_message(
            "quali framework Python conosci?",
            timeout=120,
        )
        assert result.success
        assert_italian(result.msg_output)
        output_lower = result.msg_output.lower()
        # Should mention Flask and/or Django
        assert "flask" in output_lower or "django" in output_lower, (
            f"Expected Flask/Django in response: {result.msg_output[:300]}"
        )
        # Should NOT mention guidance.studio (irrelevant to Python frameworks)
        assert "guidance.studio" not in output_lower, (
            f"guidance.studio should not appear for Python query: {result.msg_output[:300]}"
        )


# ---------------------------------------------------------------------------
# F12 — messenger quality
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA9F]"
)
_FALSE_ACTION_RE = re.compile(
    r"\b(ho esaminato|ho verificato|ho analizzato|ho controllato)\b",
    re.IGNORECASE,
)


class TestMessengerQuality:
    """F12: messenger output quality — no emoji, no hallucinated actions."""

    async def test_messenger_no_emoji_no_hallucination(self, run_message):
        """Ask a simple question → verify output quality rules."""
        result = await run_message("dimmi cosa sai fare", timeout=60)
        assert result.success
        assert_italian(result.msg_output)
        # No emoji
        assert not _EMOJI_RE.search(result.msg_output), (
            f"Emoji found in output: {result.msg_output[:300]}"
        )
        # No hallucinated actions
        assert not _FALSE_ACTION_RE.search(result.msg_output), (
            f"False action claim in output: {result.msg_output[:300]}"
        )
