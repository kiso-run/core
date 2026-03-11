"""M358: Integration tests for self-inspection flow — SSH keys, system state."""

from __future__ import annotations

import pytest

from kiso.brain import (
    build_briefer_messages,
    build_classifier_messages,
    _load_modular_prompt,
)
from kiso.store import (
    create_session,
    find_or_create_entity,
    init_db,
    save_fact,
    search_facts_by_entity,
)


class TestM358SelfInspection:
    """End-to-end prompt/data flow for self-inspection queries."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    # ── 1. Classifier routes self-inspection to "plan" ──

    def test_classifier_prompt_routes_ssh_to_plan(self):
        """Classifier system prompt contains rule to route SSH queries to plan."""
        msgs = build_classifier_messages("mostrami la tua chiave SSH pubblica")
        system = msgs[0]["content"]
        assert "SSH" in system or "system's own state" in system
        # The classifier prompt should mention self-inspection → plan
        assert "plan" in system.lower()

    def test_classifier_prompt_routes_hostname_to_plan(self):
        msgs = build_classifier_messages("what is your hostname?")
        system = msgs[0]["content"]
        assert "hostname" in system or "system's own state" in system

    def test_classifier_knowledge_question_not_special(self):
        """'What is SSH?' is a knowledge question — no self-inspection trigger."""
        msgs = build_classifier_messages("what is SSH?")
        # The user text goes in user message, not system
        user_text = msgs[1]["content"]
        assert "what is SSH?" in user_text

    # ── 2. Briefer selects entity "self" for system queries ──

    def test_briefer_prompt_mentions_self_entity(self):
        """Briefer prompt contains self-entity routing rule."""
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

    # ── 4. Planner uses shell commands, not kiso CLI ──

    def test_planner_prompt_forbids_kiso_cli_for_self_inspection(self):
        """Planner prompt says NOT to use kiso CLI for system state."""
        prompt = _load_modular_prompt("planner", [])
        assert "Do not use kiso CLI for self-inspection" in prompt

    def test_planner_prompt_suggests_shell_commands(self):
        """Planner prompt suggests standard shell commands for self-inspection."""
        prompt = _load_modular_prompt("planner", [])
        assert "cat" in prompt
        assert "hostname" in prompt

    def test_planner_knows_self_entity(self):
        """Planner prompt refers to entity 'self' in knowledge base."""
        prompt = _load_modular_prompt("planner", [])
        assert '"self"' in prompt
        assert "You ARE Kiso" in prompt
