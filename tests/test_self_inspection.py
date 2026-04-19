"""Integration tests for self-inspection flow — SSH keys, system state."""

from __future__ import annotations

import pytest

from kiso.brain import (
    build_briefer_messages,
    build_classifier_messages,
    validate_plan,
)
from kiso.store import (
    create_session,
    find_or_create_entity,
    init_db,
    save_fact,
    search_facts_by_entity,
)


class TestSelfInspection:
    """End-to-end prompt/data flow for self-inspection queries."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    # ── 1. Classifier routes self-inspection to "plan" ──

    def test_classifier_messages_include_self_inspection_query(self):
        """Self-inspection queries are passed through unchanged to the classifier."""
        msgs = build_classifier_messages("mostrami la tua chiave SSH pubblica")
        assert msgs[1]["content"] == "mostrami la tua chiave SSH pubblica"

    def test_classifier_messages_include_hostname_query(self):
        msgs = build_classifier_messages("what is your hostname?")
        assert msgs[1]["content"] == "what is your hostname?"

    def test_classifier_knowledge_question_not_special(self):
        """'What is SSH?' is a knowledge question — no self-inspection trigger."""
        msgs = build_classifier_messages("what is SSH?")
        # The user text goes in user message, not system
        user_text = msgs[1]["content"]
        assert "what is SSH?" in user_text

    # ── 2. Briefer selects entity "self" for system queries ──

    def test_briefer_prompt_mentions_self_entity(self):
        """Briefer prompt includes self-entity context when provided."""
        msgs = build_briefer_messages(
            "planner",
            "Show SSH public key",
            {"entities": "self (system)"},
        )
        system = msgs[0]["content"]
        assert 'Entity "self"' in system
        assert "this Kiso instance" in system

    def test_briefer_context_pool_includes_entities(self):
        """When entities are in context pool, they appear in briefer messages."""
        msgs = build_briefer_messages(
            "planner",
            "Show SSH public key",
            {"available_entities": "self (system)"},
        )
        user_text = msgs[1]["content"]
        assert "self (system)" in user_text

    # ── 3. Boot facts stored for entity "self" ──

    async def test_boot_facts_linked_to_self_entity(self, db):
        """Boot-style facts linked to entity 'self' are retrievable."""
        eid = await find_or_create_entity(db, "self", "system")
        await save_fact(
            db, "Instance SSH public key: ssh-ed25519 AAAA kiso@test",
            source="system", entity_id=eid,
        )
        await save_fact(
            db, "Instance runs as user 'kiso' on host 'prod-1'",
            source="system", entity_id=eid,
        )

        facts = await search_facts_by_entity(db, eid)
        assert len(facts) == 2
        contents = [f["content"] for f in facts]
        assert any("SSH" in c for c in contents)
        assert any("prod-1" in c for c in contents)

    # ── 4. Planner validation semantics for self-inspection ──

    def test_validate_plan_rejects_unknown_wrapper_task_type(self):
        """self-inspection must use exec/msg tasks — the legacy
        ``type='wrapper'`` task type is no longer recognised, so the
        validator rejects it outright as an unknown task type."""
        plan = {"tasks": [
            {"type": "wrapper", "detail": "inspect the local host", "wrapper": "kiso",
             "args": "{}", "expect": "system information returned"},
            {"type": "msg", "detail": "Answer in English. report results", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=["browser"])
        assert any("unknown type" in e for e in errors)
