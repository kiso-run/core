"""Integration tests for safety rules."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import AUTH_HEADER, DISCORD_AUTH_HEADER


# ---------------------------------------------------------------------------
# Safety fact CRUD (add/list/remove via store)
# ---------------------------------------------------------------------------


async def test_safety_fact_crud_via_store(db):
    """Add, list, and remove safety facts directly via store functions."""
    from kiso.store import save_fact, get_safety_facts

    # Initially empty
    facts = await get_safety_facts(db)
    assert facts == []

    # Add safety facts
    id1 = await save_fact(db, "Never delete production data", "admin", category="safety")
    id2 = await save_fact(db, "Always backup before migration", "admin", category="safety")

    # List returns both in order
    facts = await get_safety_facts(db)
    assert len(facts) == 2
    assert facts[0]["content"] == "Never delete production data"
    assert facts[1]["content"] == "Always backup before migration"

    # Remove one
    await db.execute("DELETE FROM facts WHERE id = ? AND category = 'safety'", (id1,))
    await db.commit()

    facts = await get_safety_facts(db)
    assert len(facts) == 1
    assert facts[0]["id"] == id2


# ---------------------------------------------------------------------------
# Safety facts injected into planner messages (always, bypassing briefer)
# ---------------------------------------------------------------------------


async def test_safety_facts_always_injected_into_planner(db, test_config):
    """Safety facts appear in planner messages regardless of briefer."""
    from kiso.store import save_fact, save_message, create_session
    from kiso.brain import build_planner_messages

    await create_session(db, "safety-planner-test")
    await save_fact(db, "Never run rm -rf /", "admin", category="safety")
    await save_fact(db, "Always use sudo carefully", "admin", category="safety")

    # Also add a non-safety fact to verify it doesn't appear in safety section
    await save_fact(db, "Project uses Python 3.12", "system", category="general")

    await save_message(db, "safety-planner-test", "testuser", "user", "deploy the app", trusted=True)

    messages, _, _ = await build_planner_messages(
        db, test_config, "safety-planner-test", "user", "deploy the app",
    )

    # Find safety rules in the messages
    all_text = " ".join(m.get("content", "") for m in messages)
    assert "Never run rm -rf /" in all_text
    assert "Always use sudo carefully" in all_text
    assert "Safety Rules" in all_text


# ---------------------------------------------------------------------------
# Behavior facts injected into planner and messenger
# ---------------------------------------------------------------------------


async def test_behavior_facts_injected_into_planner(db, test_config):
    """Behavior facts appear in planner context."""
    from kiso.store import save_fact, save_message, create_session
    from kiso.brain import build_planner_messages

    await create_session(db, "behavior-test")
    await save_fact(db, "Always respond formally", "admin", category="behavior")
    await save_message(db, "behavior-test", "testuser", "user", "do something", trusted=True)

    messages, _, _ = await build_planner_messages(
        db, test_config, "behavior-test", "user", "do something",
    )
    all_text = " ".join(m.get("content", "") for m in messages)
    assert "Always respond formally" in all_text
    assert "Behavior Guidelines" in all_text


def test_behavior_rules_in_messenger_messages():
    """Behavior rules appear in messenger context when provided."""
    from kiso.brain import build_messenger_messages
    from kiso.config import Config, Provider, SETTINGS_DEFAULTS, MODEL_DEFAULTS

    config = Config(
        tokens={"cli": "tok"}, raw={}, users={},
        providers={"openrouter": Provider(base_url="https://test.local/v1")},
        models=dict(MODEL_DEFAULTS),
        settings={**SETTINGS_DEFAULTS},
    )
    messages = build_messenger_messages(
        config, "", [], "Answer in English. hello",
        behavior_rules=["Always respond formally", "Use metrics in answers"],
    )
    all_text = " ".join(m.get("content", "") for m in messages)
    assert "Always respond formally" in all_text
    assert "Use metrics in answers" in all_text
    assert "Behavior Guidelines" in all_text


def test_no_behavior_rules_no_section():
    """When no behavior rules, the section is omitted."""
    from kiso.brain import build_messenger_messages
    from kiso.config import Config, Provider, SETTINGS_DEFAULTS, MODEL_DEFAULTS

    config = Config(
        tokens={"cli": "tok"}, raw={}, users={},
        providers={"openrouter": Provider(base_url="https://test.local/v1")},
        models=dict(MODEL_DEFAULTS),
        settings={**SETTINGS_DEFAULTS},
    )
    messages = build_messenger_messages(config, "", [], "Answer in English. hello")
    all_text = " ".join(m.get("content", "") for m in messages)
    assert "Behavior Guidelines" not in all_text


# ---------------------------------------------------------------------------
# Safety facts survive decay/cleanup
# ---------------------------------------------------------------------------


async def test_safety_facts_survive_decay(db):
    """Safety facts are excluded from decay — confidence stays at 1.0."""
    from kiso.store import save_fact, get_safety_facts, decay_facts

    sid = await save_fact(db, "Never expose credentials", "admin", category="safety")
    gid = await save_fact(db, "Project uses Flask framework", "system", category="general")

    # Backdate both facts so they'd normally decay
    await db.execute(
        "UPDATE facts SET last_used = datetime('now', '-30 days')"
    )
    await db.commit()

    decayed = await decay_facts(db, decay_days=7, decay_rate=0.1)

    # Safety fact should still be at 1.0
    cur = await db.execute("SELECT confidence FROM facts WHERE id = ?", (sid,))
    row = await cur.fetchone()
    assert row[0] == 1.0

    # General fact should have decayed
    cur = await db.execute("SELECT confidence FROM facts WHERE id = ?", (gid,))
    row = await cur.fetchone()
    assert row[0] < 1.0


# ---------------------------------------------------------------------------
# Reviewer flags safety rule violation as stuck
# ---------------------------------------------------------------------------


def test_reviewer_messages_include_safety_rules():
    """build_reviewer_messages includes safety rules section when provided."""
    from kiso.brain import build_reviewer_messages

    messages = build_reviewer_messages(
        goal="deploy the app",
        detail="run deploy script",
        expect="successful deploy",
        output="rm -rf / executed",
        user_message="deploy it",
        safety_rules=["Never run rm -rf /", "Always backup first"],
    )

    all_text = " ".join(m.get("content", "") for m in messages)
    assert "Safety Rules" in all_text
    assert "Never run rm -rf /" in all_text
    assert "Always backup first" in all_text
    assert "stuck" in all_text.lower()


def test_reviewer_messages_without_safety_rules():
    """build_reviewer_messages omits safety section when no rules."""
    from kiso.brain import build_reviewer_messages

    messages = build_reviewer_messages(
        goal="deploy the app",
        detail="run deploy script",
        expect="successful deploy",
        output="deploy completed",
        user_message="deploy it",
        safety_rules=None,
    )

    all_text = " ".join(m.get("content", "") for m in messages)
    assert "Safety Rules" not in all_text


# ---------------------------------------------------------------------------
# CLI `kiso rules` commands (API-level)
# ---------------------------------------------------------------------------


async def test_cli_rules_api_full_cycle(client: httpx.AsyncClient):
    """Full CRUD cycle through API endpoints."""
    # List — empty
    resp = await client.get("/safety-rules", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["rules"] == []

    # Add
    resp = await client.post("/safety-rules",
                             json={"content": "No destructive commands"},
                             headers=AUTH_HEADER)
    assert resp.status_code == 201
    rule_id = resp.json()["id"]
    assert resp.json()["content"] == "No destructive commands"

    # Add another
    resp = await client.post("/safety-rules",
                             json={"content": "Always use transactions"},
                             headers=AUTH_HEADER)
    assert resp.status_code == 201
    rule_id2 = resp.json()["id"]

    # List — two rules
    resp = await client.get("/safety-rules", headers=AUTH_HEADER)
    rules = resp.json()["rules"]
    assert len(rules) == 2

    # Remove first
    resp = await client.delete(f"/safety-rules/{rule_id}", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # List — one left
    resp = await client.get("/safety-rules", headers=AUTH_HEADER)
    rules = resp.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["id"] == rule_id2

    # Remove second
    resp = await client.delete(f"/safety-rules/{rule_id2}", headers=AUTH_HEADER)
    assert resp.status_code == 200

    # List — empty again
    resp = await client.get("/safety-rules", headers=AUTH_HEADER)
    assert resp.json()["rules"] == []


async def test_rules_add_empty_rejected(client: httpx.AsyncClient):
    """Empty rule content → 400."""
    resp = await client.post("/safety-rules",
                             json={"content": "  "},
                             headers=AUTH_HEADER)
    assert resp.status_code == 400


async def test_rules_delete_nonexistent_404(client: httpx.AsyncClient):
    """Deleting nonexistent rule → 404."""
    resp = await client.delete("/safety-rules/99999", headers=AUTH_HEADER)
    assert resp.status_code == 404


async def test_rules_non_admin_rejected(client: httpx.AsyncClient):
    """Non-admin token cannot add/remove rules."""
    resp = await client.post("/safety-rules",
                             json={"content": "test rule"},
                             headers=DISCORD_AUTH_HEADER)
    assert resp.status_code == 403

    resp = await client.delete("/safety-rules/1", headers=DISCORD_AUTH_HEADER)
    assert resp.status_code == 403
