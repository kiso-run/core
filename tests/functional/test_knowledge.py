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
        assert "skill" not in types
        # Hostname appears in output
        assert platform.node().lower() in result.msg_output.lower()


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
