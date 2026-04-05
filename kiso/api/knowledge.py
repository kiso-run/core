"""Knowledge and safety API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

import kiso.main as main_mod

router = APIRouter()


class SafetyRuleRequest(BaseModel):
    content: str


class KnowledgeRequest(BaseModel):
    content: str
    category: str = "general"
    entity_name: str | None = None
    entity_kind: str | None = None
    tags: list[str] | None = None
    project_id: int | None = None


@router.get("/safety-rules")
async def list_safety_rules(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    facts = await main_mod.get_safety_facts(request.app.state.db)
    return {"rules": facts}


@router.post("/safety-rules", status_code=201)
async def add_safety_rule(
    body: SafetyRuleRequest,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Rule content cannot be empty")
    fact_id = await main_mod.save_fact(request.app.state.db, content, "admin", category="safety")
    return {"id": fact_id, "content": content}


@router.delete("/safety-rules/{rule_id}")
async def delete_safety_rule(
    rule_id: int,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    cur = await db.execute("DELETE FROM facts WHERE id = ? AND category = 'safety'", (rule_id,))
    await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Safety rule not found")
    return {"deleted": True, "id": rule_id}


@router.get("/knowledge")
async def list_knowledge_endpoint(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    category: str | None = None,
    entity: str | None = None,
    tag: str | None = None,
    search: str | None = None,
    limit: int = 50,
):
    facts = await main_mod.list_knowledge(
        request.app.state.db,
        category=category,
        entity=entity,
        tag=tag,
        search=search,
        limit=limit,
    )
    return {"facts": facts}


@router.post("/knowledge", status_code=201)
async def add_knowledge(
    body: KnowledgeRequest,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if body.category not in main_mod._VALID_FACT_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category: {body.category}")

    entity_id = None
    if body.entity_name:
        from kiso.store import find_or_create_entity

        kind = body.entity_kind or "concept"
        entity_id = await find_or_create_entity(db, body.entity_name, kind)

    try:
        fact_id = await main_mod.save_fact(
            db,
            content,
            "admin",
            category=body.category,
            tags=body.tags,
            entity_id=entity_id,
            project_id=body.project_id,
        )
    except Exception as e:
        if "FOREIGN KEY constraint" in str(e):
            raise HTTPException(status_code=400, detail="Invalid project_id — project not found")
        raise
    return {"id": fact_id, "content": content, "category": body.category}


@router.delete("/knowledge/{fact_id}")
async def delete_knowledge(
    fact_id: int,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    expected_category: str | None = None,
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    cur = await db.execute("SELECT category FROM facts WHERE id = ?", (fact_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Fact not found")
    if row["category"] == "safety":
        raise HTTPException(status_code=400, detail="Use /safety-rules to manage safety rules")
    if expected_category and row["category"] != expected_category:
        raise HTTPException(
            status_code=400,
            detail=f"Fact {fact_id} is category '{row['category']}', expected '{expected_category}'",
        )
    await db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    await db.commit()
    return {"deleted": True, "id": fact_id}
