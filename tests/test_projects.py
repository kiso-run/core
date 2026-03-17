"""M683-M689: Tests for project management."""

from __future__ import annotations

import pytest

from kiso.store import (
    add_project_member,
    create_project,
    create_session,
    delete_project,
    get_project,
    get_user_project_role,
    init_db,
    list_project_members,
    list_projects,
    remove_project_member,
)


@pytest.fixture()
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


# --- M683: Project CRUD ---


async def test_create_project(db):
    pid = await create_project(db, "my-app", "alice", description="Main app")
    assert isinstance(pid, int)
    assert pid > 0


async def test_get_project(db):
    await create_project(db, "my-app", "alice", description="Main app")
    proj = await get_project(db, "my-app")
    assert proj is not None
    assert proj["name"] == "my-app"
    assert proj["description"] == "Main app"
    assert proj["created_by"] == "alice"


async def test_get_project_not_found(db):
    assert await get_project(db, "nonexistent") is None


async def test_list_projects_all(db):
    await create_project(db, "proj-a", "alice")
    await create_project(db, "proj-b", "bob")
    projects = await list_projects(db)
    assert len(projects) == 2


async def test_list_projects_by_user(db):
    pid_a = await create_project(db, "proj-a", "alice")
    pid_b = await create_project(db, "proj-b", "bob")
    # Alice is auto-member of proj-a, bob of proj-b
    projects = await list_projects(db, username="alice")
    assert len(projects) == 1
    assert projects[0]["name"] == "proj-a"


async def test_delete_project(db):
    pid = await create_project(db, "to-delete", "alice")
    assert await delete_project(db, pid) is True
    assert await get_project(db, "to-delete") is None


async def test_delete_project_cascades_members(db):
    pid = await create_project(db, "cascade-test", "alice")
    await add_project_member(db, pid, "bob")
    await delete_project(db, pid)
    members = await list_project_members(db, pid)
    assert members == []


# --- M683: Project members ---


async def test_creator_is_auto_member(db):
    pid = await create_project(db, "auto-member", "alice")
    members = await list_project_members(db, pid)
    assert len(members) == 1
    assert members[0]["username"] == "alice"
    assert members[0]["role"] == "member"


async def test_add_project_member(db):
    pid = await create_project(db, "team", "alice")
    await add_project_member(db, pid, "bob", role="member")
    await add_project_member(db, pid, "carol", role="viewer")
    members = await list_project_members(db, pid)
    assert len(members) == 3
    roles = {m["username"]: m["role"] for m in members}
    assert roles["alice"] == "member"
    assert roles["bob"] == "member"
    assert roles["carol"] == "viewer"


async def test_add_member_updates_role(db):
    pid = await create_project(db, "role-update", "alice")
    await add_project_member(db, pid, "bob", role="viewer")
    assert await get_user_project_role(db, pid, "bob") == "viewer"
    await add_project_member(db, pid, "bob", role="member")
    assert await get_user_project_role(db, pid, "bob") == "member"


async def test_remove_project_member(db):
    pid = await create_project(db, "remove-test", "alice")
    await add_project_member(db, pid, "bob")
    assert await remove_project_member(db, pid, "bob") is True
    members = await list_project_members(db, pid)
    assert len(members) == 1  # only alice remains


async def test_remove_nonexistent_member(db):
    pid = await create_project(db, "remove-fail", "alice")
    assert await remove_project_member(db, pid, "nobody") is False


async def test_get_user_project_role(db):
    pid = await create_project(db, "role-test", "alice")
    assert await get_user_project_role(db, pid, "alice") == "member"
    assert await get_user_project_role(db, pid, "nobody") is None


async def test_user_in_multiple_projects(db):
    pid_a = await create_project(db, "proj-a", "alice")
    pid_b = await create_project(db, "proj-b", "bob")
    await add_project_member(db, pid_b, "alice", role="viewer")
    projects = await list_projects(db, username="alice")
    assert len(projects) == 2
    names = {p["name"] for p in projects}
    assert names == {"proj-a", "proj-b"}
