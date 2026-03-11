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
