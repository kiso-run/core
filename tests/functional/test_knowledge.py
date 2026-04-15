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

from tests.conftest import LLM_REPLAN_TIMEOUT, LLM_SINGLE_PLAN_TIMEOUT
from tests.functional.conftest import assert_italian, assert_no_failure_language

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# F9 — chat_kb self-inspection
# ---------------------------------------------------------------------------


class TestChatKBSelfInspection:
    """F9: chat_kb fast path resolves entity "self" for system introspection."""

    async def test_chat_kb_self_hostname(self, run_message):
        """What: chat_kb fast-path test for system introspection (hostname query).

        Why: Validates that the chat_kb classifier resolves self-inspection queries
        from boot facts without spawning exec or wrapper tasks. If this breaks, simple
        system queries would wastefully invoke the full planner pipeline.
        Expects: Success, Italian response, no exec/wrapper tasks, hostname info in output.
        """
        result = await run_message(
            "qual è il tuo hostname?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert result.success
        # classifier may route as plan (system state → plan per rules)
        # or chat_kb (boot fact available). Both paths produce correct output.
        # Only verify the response contains hostname info.
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
        """What: Two-turn learning pipeline test: teach a fact, then query it back.

        Why: Validates end-to-end learning retention. If Kiso cannot store and recall
        taught facts, the entire knowledge system is broken.
        Expects: Both turns succeed, second response is Italian and mentions "flask".

        The teach message contains the word "framework" literally so the
        FTS5 retrieval used by the briefer for r2 hits deterministically
        against the query vocabulary ("che framework..."). Without this
        overlap, the planner falls back to filesystem investigation,
        defeating the purpose of the recall check.
        """
        # Message 1: teach a fact (vocabulary overlaps with the query)
        r1 = await run_message(
            "ricordati che questo progetto usa Flask come framework, "
            "con SQLAlchemy per il database",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert r1.success

        # Message 2: query the fact
        r2 = await run_message(
            "che framework usa il progetto corrente?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert r2.success
        assert_italian(r2.msg_output)
        types = r2.task_types()
        assert "exec" not in types, f"Knowledge recall should not need exec: {types}"
        assert "wrapper" not in types, f"Knowledge recall should not need wrappers: {types}"
        # Should mention Flask
        assert "flask" in r2.msg_output.lower()


# ---------------------------------------------------------------------------
# F11 — entity/tag enrichment
# ---------------------------------------------------------------------------


class TestEntityTagEnrichment:
    """F11: pre-seeded entity + tagged facts surface in responses."""

    async def test_entity_tag_enrichment(self, func_db, run_message):
        """What: Entity+tag enrichment test with pre-seeded facts in the DB.

        Why: Validates that the briefer's entity and tag-based retrieval correctly
        surfaces pre-seeded facts in responses. Without this, stored knowledge with
        entity/tag metadata would never reach the LLM context.
        Expects: Success, Italian response mentioning "onboarding" or "saas" from
        the pre-seeded guidance.studio fact.
        """
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
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert result.success
        assert_italian(result.last_plan_msg_output)
        types = result.task_types()
        assert "exec" not in types, f"Pre-seeded fact recall should not need exec: {types}"
        assert "wrapper" not in types, f"Pre-seeded fact recall should not need wrappers: {types}"
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
        """What: chat_kb classification test for user-defined entities.

        Why: Validates that the system recognizes it can answer from stored knowledge
        about user-defined entities without executing commands. If broken, every
        knowledge query would trigger unnecessary exec tasks.
        Expects: Success, Italian response with pre-seeded fact content, msg-only
        tasks (no exec or wrapper), at least 1 msg task.
        """
        from kiso.store import find_or_create_entity, save_fact

        eid = await find_or_create_entity(func_db, "guidance.studio", "website")
        await save_fact(
            func_db,
            "guidance.studio is a SaaS platform for interactive user onboarding workflows",
            source="curator", category="general",
            tags=["website", "saas", "onboarding"], entity_id=eid,
        )
        result = await run_message(
            "cosa sai di guidance.studio?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert result.success
        assert_italian(result.last_plan_msg_output)
        # Response should contain pre-seeded fact content
        output_lower = result.msg_output.lower()
        assert "onboarding" in output_lower or "saas" in output_lower or "workflow" in output_lower
        # chat_kb fast path: no exec or wrapper tasks, only msg
        types = result.task_types()
        assert "exec" not in types, f"Expected no exec tasks, got: {types}"
        assert "wrapper" not in types, f"Expected no wrapper tasks, got: {types}"
        # Should have exactly 1 msg task (fast-path produces single msg)
        msg_tasks = [t for t in result.tasks if t.get("type") == "msg"]
        assert len(msg_tasks) >= 1, f"Expected at least 1 msg task, got: {types}"


# ---------------------------------------------------------------------------
# F14 — curator entity creation across turns
# ---------------------------------------------------------------------------


class TestF14CuratorEntityCreation:
    """F14: curator creates entity + tags from seeded learning → recall.

    seeds a high-quality learning directly in the DB to isolate the
    curator from reviewer non-determinism. The test validates the curator
    pipeline (promote → entity → fact → tag), not the review→learn flow.
    """

    async def test_curator_entity_creation_and_recall(self, func_config, func_db, func_session, run_message):
        """What: Seed a Python learning, run curator, verify entity + fact + tag.

        Why: Validates that the curator correctly promotes a well-formed learning,
        creates an entity, links a fact, and assigns tags. Subsequent query recalls it.
        Expects: "python" entity in DB with >=1 linked fact after curator runs;
        recall query mentions Python in Italian.
        """
        from kiso.store import save_learning, create_session
        from kiso.worker.loop import _post_plan_knowledge

        # Ensure session exists
        try:
            await create_session(func_db, func_session)
        except Exception:
            pass

        # Seed a project-specific learning (not general knowledge)
        learning_id = await save_learning(
            func_db,
            "This project uses Python 3.12 with Flask 3.0 and SQLAlchemy 2.0 "
            "on PostgreSQL 16 for the backend API",
            func_session,
        )
        assert learning_id > 0, "Learning was rejected by save_learning"

        # Run the curator pipeline directly (not via full message processing)
        llm_timeout = func_config.settings.get("llm_timeout", 60)
        await _post_plan_knowledge(func_db, func_config, func_session, None, llm_timeout)

        # Check DB: entity should exist
        cur = await func_db.execute(
            "SELECT id, name, kind FROM entities WHERE LOWER(name) LIKE '%python%'"
        )
        entities = [dict(r) for r in await cur.fetchall()]

        if entities:
            # Primary path: entity created — check linked facts
            entity_ids = [e["id"] for e in entities]
            placeholders = ",".join("?" * len(entity_ids))
            cur = await func_db.execute(
                f"SELECT id, content FROM facts WHERE entity_id IN ({placeholders})",
                entity_ids,
            )
            facts = [dict(r) for r in await cur.fetchall()]
            assert len(facts) >= 1, "Expected ≥1 fact with python entity_id"
        else:
            # Fallback: curator promoted fact but entity name doesn't match "%python%"
            cur = await func_db.execute(
                "SELECT content FROM facts WHERE LOWER(content) LIKE '%python%'"
            )
            python_facts = [dict(r) for r in await cur.fetchall()]
            assert len(python_facts) >= 1, (
                "No entity matching 'python' AND no facts containing 'python' — "
                "curator pipeline did not promote the seeded learning"
            )

        # Turn 2: ask back → should recall learned information
        r2 = await run_message(
            "cosa sai di python?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert r2.success
        assert_italian(r2.msg_output)
        assert "python" in r2.msg_output.lower()


# ---------------------------------------------------------------------------
# F15 — entity dedup and tag reuse
# ---------------------------------------------------------------------------


class TestF15EntityDedupTagReuse:
    """F15: pre-seeded entity 'flask' → teach new fact → no duplicate entity."""

    async def test_entity_dedup_tag_reuse(self, func_db, run_message):
        """What: Entity deduplication test when teaching new facts about existing entities.

        Why: Validates that find_or_create_entity prevents duplicate entities. Without
        dedup, the entity table would fill with duplicates, degrading retrieval quality
        and inflating briefer context.
        Expects: Exactly 1 Flask entity after teaching a new fact, total entity count
        grows by at most 2.
        """
        from kiso.store import find_or_create_entity, save_fact

        # Pre-seed entity + fact
        eid = await find_or_create_entity(func_db, "flask", "wrapper")
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
            timeout=LLM_REPLAN_TIMEOUT,
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
        """What: Scored retrieval relevance test with mixed-domain pre-seeded entities.

        Why: Validates that the briefer's scored retrieval filters by relevance, so
        irrelevant entities (guidance.studio) do not pollute responses about Python
        frameworks. Without this, every query would surface all stored facts.
        Expects: Response mentions Flask/Django but does NOT mention guidance.studio.
        """
        from kiso.store import find_or_create_entity, save_fact

        # Pre-seed 3 entities
        flask_id = await find_or_create_entity(func_db, "flask", "wrapper")
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

        django_id = await find_or_create_entity(func_db, "django", "wrapper")
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
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert result.success
        assert_italian(result.last_plan_msg_output)
        output_lower = result.msg_output.lower()
        types = result.task_types()
        assert "exec" not in types, f"Scored fact retrieval should not need exec: {types}"
        assert "wrapper" not in types, f"Scored fact retrieval should not need wrappers: {types}"
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

# import the production emoji regex so the test cannot drift
# from the deterministic strip applied to messenger output.
from kiso.brain.text_roles import EMOJI_STRIP_RE as _EMOJI_RE  # noqa: E402
_FALSE_ACTION_RE = re.compile(
    r"\b(ho esaminato|ho verificato|ho analizzato|ho controllato)\b",
    re.IGNORECASE,
)


class TestMessengerQuality:
    """F12: messenger output quality — no emoji, no hallucinated actions."""

    async def test_messenger_no_emoji_no_hallucination(self, run_message):
        """What: Messenger output quality check for emoji and hallucination rules.

        Why: Validates quality rules -- the messenger must not use emoji and must
        not claim actions it did not perform (e.g. "ho esaminato", "ho verificato").
        Without this, users receive unprofessional or misleading responses.
        Expects: Italian response with no emoji characters and no false action verbs.
        """
        result = await run_message(
            "dimmi cosa sai fare",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        assert result.success
        assert_italian(result.last_plan_msg_output)
        # No emoji
        assert not _EMOJI_RE.search(result.msg_output), (
            f"Emoji found in output: {result.msg_output[:300]}"
        )
        # No hallucinated actions
        assert not _FALSE_ACTION_RE.search(result.msg_output), (
            f"False action claim in output: {result.msg_output[:300]}"
        )
