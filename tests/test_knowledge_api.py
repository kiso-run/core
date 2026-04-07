"""Integration tests for /knowledge API endpoints."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import AUTH_HEADER, DISCORD_AUTH_HEADER


# ---------------------------------------------------------------------------
# GET /knowledge
# ---------------------------------------------------------------------------


async def test_list_knowledge_empty(client: httpx.AsyncClient):
    resp = await client.get("/knowledge", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["facts"] == []


async def test_list_knowledge_with_facts(client: httpx.AsyncClient):
    # Add two facts
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "Project uses Flask", "category": "project"})
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "Always be concise", "category": "behavior"})

    resp = await client.get("/knowledge", headers=AUTH_HEADER)
    assert resp.status_code == 200
    facts = resp.json()["facts"]
    assert len(facts) >= 2


async def test_list_knowledge_filter_by_category(client: httpx.AsyncClient):
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "Fact A general", "category": "general"})
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "Fact B behavior", "category": "behavior"})

    resp = await client.get("/knowledge", headers=AUTH_HEADER,
                            params={"category": "behavior"})
    facts = resp.json()["facts"]
    assert all(f["category"] == "behavior" for f in facts)
    assert any("Fact B" in f["content"] for f in facts)


async def test_list_knowledge_filter_by_tag(client: httpx.AsyncClient):
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "Flask is a Python framework",
                            "category": "tool", "tags": ["python", "flask"]})
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "React is a JS framework",
                            "category": "tool", "tags": ["javascript", "react"]})

    resp = await client.get("/knowledge", headers=AUTH_HEADER,
                            params={"tag": "python"})
    facts = resp.json()["facts"]
    assert any("Flask" in f["content"] for f in facts)
    assert not any("React" in f["content"] for f in facts)


async def test_list_knowledge_search(client: httpx.AsyncClient):
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "PostgreSQL database is used for production"})
    await client.post("/knowledge", headers=AUTH_HEADER,
                      json={"content": "Redis used for caching sessions"})

    resp = await client.get("/knowledge", headers=AUTH_HEADER,
                            params={"search": "PostgreSQL production"})
    facts = resp.json()["facts"]
    assert len(facts) >= 1
    assert any("PostgreSQL" in f["content"] for f in facts)


# ---------------------------------------------------------------------------
# POST /knowledge
# ---------------------------------------------------------------------------


async def test_add_knowledge_basic(client: httpx.AsyncClient):
    resp = await client.post("/knowledge", headers=AUTH_HEADER,
                             json={"content": "The project uses microservices"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["content"] == "The project uses microservices"
    assert data["category"] == "general"  # default


async def test_add_knowledge_with_entity_and_tags(client: httpx.AsyncClient):
    resp = await client.post("/knowledge", headers=AUTH_HEADER, json={
        "content": "Backend API runs on port 8000",
        "category": "project",
        "entity_name": "backend-api",
        "entity_kind": "project",
        "tags": ["backend", "api", "config"],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["category"] == "project"

    # Verify it shows up with entity filter
    resp = await client.get("/knowledge", headers=AUTH_HEADER,
                            params={"entity": "backend-api"})
    facts = resp.json()["facts"]
    assert any("port 8000" in f["content"] for f in facts)
    assert any("backend" in f.get("tags", []) for f in facts)


async def test_add_knowledge_empty_content_rejected(client: httpx.AsyncClient):
    resp = await client.post("/knowledge", headers=AUTH_HEADER,
                             json={"content": "  "})
    assert resp.status_code == 400


async def test_add_knowledge_invalid_category_rejected(client: httpx.AsyncClient):
    resp = await client.post("/knowledge", headers=AUTH_HEADER,
                             json={"content": "Some fact", "category": "invalid"})
    assert resp.status_code == 400


async def test_add_knowledge_non_admin_rejected(client: httpx.AsyncClient):
    resp = await client.post("/knowledge", headers=DISCORD_AUTH_HEADER,
                             json={"content": "Some fact"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /knowledge
# ---------------------------------------------------------------------------


async def test_delete_knowledge(client: httpx.AsyncClient):
    resp = await client.post("/knowledge", headers=AUTH_HEADER,
                             json={"content": "Temporary fact to delete"})
    fact_id = resp.json()["id"]

    resp = await client.delete(f"/knowledge/{fact_id}", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify it's gone
    resp = await client.get("/knowledge", headers=AUTH_HEADER)
    facts = resp.json()["facts"]
    assert not any(f["id"] == fact_id for f in facts)


async def test_delete_knowledge_not_found(client: httpx.AsyncClient):
    resp = await client.delete("/knowledge/99999", headers=AUTH_HEADER)
    assert resp.status_code == 404


async def test_delete_safety_via_knowledge_rejected(client: httpx.AsyncClient):
    """Safety rules must be managed via /safety-rules, not /knowledge."""
    resp = await client.post("/safety-rules", headers=AUTH_HEADER,
                             json={"content": "Never delete data"})
    rule_id = resp.json()["id"]

    resp = await client.delete(f"/knowledge/{rule_id}", headers=AUTH_HEADER)
    assert resp.status_code == 400
    assert "safety-rules" in resp.json()["detail"]


async def test_delete_knowledge_non_admin_rejected(client: httpx.AsyncClient):
    resp = await client.post("/knowledge", headers=AUTH_HEADER,
                             json={"content": "Fact to protect"})
    fact_id = resp.json()["id"]

    resp = await client.delete(f"/knowledge/{fact_id}", headers=DISCORD_AUTH_HEADER)
    assert resp.status_code == 403
