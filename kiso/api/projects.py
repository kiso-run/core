"""Project API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

import kiso.main as main_mod

router = APIRouter()


class ProjectRequest(BaseModel):
    name: str
    description: str = ""


class ProjectMemberRequest(BaseModel):
    username: str
    role: str = "member"


@router.get("/projects")
async def list_projects_endpoint(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    user: str = Query(...),
):
    db = request.app.state.db
    resolved = main_mod.resolve_user(request.app.state.config, user, auth.token_name)
    if main_mod._is_admin(resolved):
        projects = await main_mod.list_projects(db)
    else:
        projects = await main_mod.list_projects(db, username=resolved.username)
    return {"projects": projects}


@router.post("/projects", status_code=201)
async def create_project_endpoint(
    body: ProjectRequest,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty")
    existing = await main_mod.get_project(db, name)
    if existing:
        raise HTTPException(status_code=409, detail=f"Project '{name}' already exists")
    project_id = await main_mod.create_project(db, name, "admin", description=body.description)
    return {"id": project_id, "name": name}


@router.get("/projects/{name}")
async def get_project_endpoint(
    name: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    db = request.app.state.db
    project = await main_mod.get_project(db, name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    members = await main_mod.list_project_members(db, project["id"])
    return {"project": project, "members": members}


@router.delete("/projects/{name}")
async def delete_project_endpoint(
    name: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    project = await main_mod.get_project(db, name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await main_mod.delete_project(db, project["id"])
    return {"deleted": True, "name": name}


@router.post("/projects/{name}/bind/{session}")
async def bind_project_session(
    name: str,
    session: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    project = await main_mod.get_project(db, name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    sess = await main_mod.get_session(db, session)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    await main_mod.bind_session_to_project(db, session, project["id"])
    return {"bound": True, "session": session, "project": name}


@router.post("/projects/{name}/unbind/{session}")
async def unbind_project_session(
    name: str,
    session: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    await main_mod.unbind_session_from_project(request.app.state.db, session)
    return {"unbound": True, "session": session}


@router.post("/projects/{name}/members")
async def add_project_member_endpoint(
    name: str,
    body: ProjectMemberRequest,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    project = await main_mod.get_project(db, name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if body.role not in ("member", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be 'member' or 'viewer'")
    await main_mod.add_project_member(db, project["id"], body.username, role=body.role)
    return {"added": True, "username": body.username, "role": body.role}


@router.delete("/projects/{name}/members/{username}")
async def remove_project_member_endpoint(
    name: str,
    username: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    project = await main_mod.get_project(db, name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    removed = await main_mod.remove_project_member(db, project["id"], username)
    if not removed:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"removed": True, "username": username}


@router.get("/projects/{name}/members")
async def list_project_members_endpoint(
    name: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    db = request.app.state.db
    project = await main_mod.get_project(db, name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    members = await main_mod.list_project_members(db, project["id"])
    return {"members": members}
