"""M683-M689: Tests for project management."""

from __future__ import annotations

import pytest

from kiso.store import (
    add_project_member,
    bind_session_to_project,
    create_project,
    create_session,
    delete_project,
    get_facts,
    get_project,
    get_session_project_id,
    get_user_project_role,
    init_db,
    list_project_members,
    list_projects,
    remove_project_member,
    save_fact,
    search_facts,
    search_facts_by_tags,
    save_fact_tags,
    unbind_session_from_project,
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


# --- M684: Session-project binding ---


async def test_bind_session_to_project(db):
    pid = await create_project(db, "bind-test", "alice")
    await create_session(db, "sess1")
    await bind_session_to_project(db, "sess1", pid)
    assert await get_session_project_id(db, "sess1") == pid


async def test_unbind_session_from_project(db):
    pid = await create_project(db, "unbind-test", "alice")
    await create_session(db, "sess1")
    await bind_session_to_project(db, "sess1", pid)
    await unbind_session_from_project(db, "sess1")
    assert await get_session_project_id(db, "sess1") is None


async def test_session_project_id_default_none(db):
    await create_session(db, "sess1")
    assert await get_session_project_id(db, "sess1") is None


# --- M685: Fact visibility scoping (3-level query) ---


async def test_fact_global_visible_to_all(db):
    """Global facts (no project_id, non-user category) are visible to everyone."""
    await save_fact(db, "Global tool fact for everyone", "system", category="tool")
    # Non-admin, with username — should see global facts
    facts = await get_facts(db, session="sess1", username="bob")
    assert any("Global tool fact" in f["content"] for f in facts)


async def test_fact_project_scoped_visible_to_member(db):
    """Project-scoped facts visible to project members."""
    pid = await create_project(db, "proj-x", "alice")
    await add_project_member(db, pid, "bob", role="member")
    await save_fact(db, "Project X specific behavior guideline", "curator",
                    category="project", project_id=pid)
    # Bob is a member — should see the fact
    facts = await get_facts(db, session="sess1", username="bob")
    assert any("Project X specific" in f["content"] for f in facts)


async def test_fact_project_scoped_invisible_to_nonmember(db):
    """Project-scoped facts NOT visible to non-members."""
    pid = await create_project(db, "proj-x", "alice")
    await save_fact(db, "Secret project X fact content", "curator",
                    category="project", project_id=pid)
    # Charlie is NOT a member — should NOT see the fact
    facts = await get_facts(db, session="sess1", username="charlie")
    assert not any("Secret project X fact" in f["content"] for f in facts)


async def test_fact_user_category_session_scoped(db):
    """User-category facts visible only in their session."""
    await save_fact(db, "User preference for dark mode colors", "curator",
                    category="user", session="sess1")
    # Same session — visible
    facts = await get_facts(db, session="sess1", username="bob")
    assert any("dark mode" in f["content"] for f in facts)
    # Different session — not visible
    facts = await get_facts(db, session="sess2", username="bob")
    assert not any("dark mode" in f["content"] for f in facts)


async def test_fact_admin_sees_all(db):
    """Admin bypasses all scoping — sees everything."""
    pid = await create_project(db, "proj-secret", "alice")
    await save_fact(db, "Secret admin-only project fact", "curator",
                    category="project", project_id=pid)
    await save_fact(db, "User session-only fact content here", "curator",
                    category="user", session="sess1")
    # Admin sees all
    facts = await get_facts(db, is_admin=True)
    assert any("Secret admin-only" in f["content"] for f in facts)
    assert any("User session-only" in f["content"] for f in facts)


async def test_search_facts_respects_project_scope(db):
    """search_facts respects project scoping."""
    pid = await create_project(db, "proj-search", "alice")
    await save_fact(db, "Searchable project-scoped knowledge fact", "curator",
                    category="project", project_id=pid)
    # Non-member should not find it
    results = await search_facts(db, "searchable project", session="s1", username="bob")
    assert not any("Searchable project-scoped" in f["content"] for f in results)
    # Member should find it
    await add_project_member(db, pid, "bob")
    results = await search_facts(db, "searchable project", session="s1", username="bob")
    assert any("Searchable project-scoped" in f["content"] for f in results)


async def test_search_facts_by_tags_respects_project_scope(db):
    """search_facts_by_tags respects project scoping."""
    pid = await create_project(db, "proj-tags", "alice")
    fid = await save_fact(db, "Tagged project fact for tag search test", "curator",
                          category="project", project_id=pid, tags=["special-tag"])
    # Non-member should not find it
    results = await search_facts_by_tags(db, ["special-tag"], session="s1", username="bob")
    assert not any("Tagged project fact" in f["content"] for f in results)
    # Member should find it
    await add_project_member(db, pid, "bob")
    results = await search_facts_by_tags(db, ["special-tag"], session="s1", username="bob")
    assert any("Tagged project fact" in f["content"] for f in results)


async def test_fact_no_username_legacy_filter(db):
    """Without username, legacy 2-level filter applies (user category filtered by session)."""
    await save_fact(db, "Legacy user-scoped fact in session", "curator",
                    category="user", session="sess1")
    await save_fact(db, "Legacy global general fact content", "curator", category="general")
    # Legacy filter: no username
    facts = await get_facts(db, session="sess1")
    assert any("Legacy user-scoped" in f["content"] for f in facts)
    assert any("Legacy global general" in f["content"] for f in facts)
    # Different session — user fact not visible
    facts = await get_facts(db, session="sess2")
    assert not any("Legacy user-scoped" in f["content"] for f in facts)


# --- M686: API access control tests ---


@pytest.fixture()
def _app():
    """Create a test app instance."""
    from kiso.main import app
    return app


async def test_require_project_role_no_project(db):
    """No project attached — no restriction."""
    from kiso.main import _require_project_role
    await create_session(db, "open-sess")
    # Should not raise
    await _require_project_role(db, "open-sess", "anyone")


async def test_require_project_role_member_allowed(db):
    """Member can access with min_role=member."""
    from kiso.main import _require_project_role
    pid = await create_project(db, "access-test", "alice")
    await create_session(db, "proj-sess")
    await bind_session_to_project(db, "proj-sess", pid)
    # Alice is member — should pass
    await _require_project_role(db, "proj-sess", "alice", min_role="member")


async def test_require_project_role_viewer_blocked_for_member(db):
    """Viewer cannot access with min_role=member."""
    from fastapi import HTTPException
    from kiso.main import _require_project_role
    pid = await create_project(db, "viewer-block", "alice")
    await add_project_member(db, pid, "bob", role="viewer")
    await create_session(db, "proj-sess2")
    await bind_session_to_project(db, "proj-sess2", pid)
    with pytest.raises(HTTPException) as exc_info:
        await _require_project_role(db, "proj-sess2", "bob", min_role="member")
    assert exc_info.value.status_code == 403


async def test_require_project_role_nonmember_blocked(db):
    """Non-member cannot access project session."""
    from fastapi import HTTPException
    from kiso.main import _require_project_role
    pid = await create_project(db, "nonmember-block", "alice")
    await create_session(db, "proj-sess3")
    await bind_session_to_project(db, "proj-sess3", pid)
    with pytest.raises(HTTPException) as exc_info:
        await _require_project_role(db, "proj-sess3", "stranger", min_role="viewer")
    assert exc_info.value.status_code == 403


async def test_require_project_role_viewer_allowed_for_viewer(db):
    """Viewer can access with min_role=viewer."""
    from kiso.main import _require_project_role
    pid = await create_project(db, "viewer-ok", "alice")
    await add_project_member(db, pid, "bob", role="viewer")
    await create_session(db, "proj-sess4")
    await bind_session_to_project(db, "proj-sess4", pid)
    # Should not raise
    await _require_project_role(db, "proj-sess4", "bob", min_role="viewer")


# --- M687/M688: CLI project commands ---


def test_cli_project_parser():
    """Verify project subcommand parser is registered."""
    from cli import build_parser
    parser = build_parser()
    # Should parse without error
    args = parser.parse_args(["project", "list"])
    assert args.command == "project"
    assert args.project_cmd == "list"


def test_cli_project_create_parser():
    """Verify project create parser accepts name and description."""
    from cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["project", "create", "my-app", "--description", "A test project"])
    assert args.name == "my-app"
    assert args.description == "A test project"


def test_cli_project_bind_parser():
    """Verify project bind parser accepts session and project."""
    from cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["project", "bind", "sess1", "my-app"])
    assert args.session == "sess1"
    assert args.project == "my-app"


def test_cli_project_add_member_parser():
    """Verify project add-member parser accepts username and project."""
    from cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["project", "add-member", "bob", "--project", "my-app", "--role", "viewer"])
    assert args.username == "bob"
    assert args.project == "my-app"
    assert args.role == "viewer"


def test_cli_project_members_parser():
    """Verify project members parser accepts project."""
    from cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["project", "members", "--project", "my-app"])
    assert args.project == "my-app"


# --- M689: Curator project-awareness ---


async def test_curator_project_scoped_fact(db):
    """Facts with category project/behavior get project_id from session."""
    from unittest.mock import AsyncMock, patch
    from kiso.worker.loop import _apply_curator_result

    pid = await create_project(db, "curator-proj", "alice")
    await create_session(db, "cur-sess")
    await bind_session_to_project(db, "cur-sess", pid)

    # Save a learning so we can reference it
    from kiso.store import save_learning
    lid = await save_learning(db, "Project X uses React framework for testing", "cur-sess", "alice")

    result = {
        "evaluations": [
            {
                "learning_id": lid,
                "verdict": "promote",
                "fact": "Project X uses React framework for frontend",
                "category": "project",
                "tags": ["react"],
            },
        ]
    }
    await _apply_curator_result(db, "cur-sess", result)
    # The promoted fact should have project_id set
    facts = await get_facts(db, is_admin=True)
    proj_facts = [f for f in facts if "React framework" in f["content"]]
    assert len(proj_facts) == 1
    assert proj_facts[0]["project_id"] == pid


async def test_curator_global_fact_stays_global(db):
    """Facts with category general/tool/system remain global (no project_id)."""
    from kiso.worker.loop import _apply_curator_result

    pid = await create_project(db, "curator-proj2", "alice")
    await create_session(db, "cur-sess2")
    await bind_session_to_project(db, "cur-sess2", pid)

    from kiso.store import save_learning
    lid = await save_learning(db, "Python asyncio is used for concurrent operations", "cur-sess2", "alice")

    result = {
        "evaluations": [
            {
                "learning_id": lid,
                "verdict": "promote",
                "fact": "Python asyncio enables concurrent operations in this codebase",
                "category": "general",
                "tags": ["python"],
            },
        ]
    }
    await _apply_curator_result(db, "cur-sess2", result)
    facts = await get_facts(db, is_admin=True)
    gen_facts = [f for f in facts if "asyncio enables" in f["content"]]
    assert len(gen_facts) == 1
    assert gen_facts[0]["project_id"] is None


async def test_curator_behavior_gets_project_id(db):
    """Behavior category facts get project_id when session has project."""
    from kiso.worker.loop import _apply_curator_result

    pid = await create_project(db, "curator-proj3", "alice")
    await create_session(db, "cur-sess3")
    await bind_session_to_project(db, "cur-sess3", pid)

    from kiso.store import save_learning
    lid = await save_learning(db, "Always use descriptive variable names in code", "cur-sess3", "alice")

    result = {
        "evaluations": [
            {
                "learning_id": lid,
                "verdict": "promote",
                "fact": "Code style: always use descriptive variable names here",
                "category": "behavior",
            },
        ]
    }
    await _apply_curator_result(db, "cur-sess3", result)
    facts = await get_facts(db, is_admin=True)
    beh_facts = [f for f in facts if "descriptive variable" in f["content"]]
    assert len(beh_facts) == 1
    assert beh_facts[0]["project_id"] == pid


async def test_curator_no_project_session_stays_global(db):
    """When session has no project, all facts stay global."""
    from kiso.worker.loop import _apply_curator_result

    await create_session(db, "no-proj-sess")

    from kiso.store import save_learning
    lid = await save_learning(db, "This project uses Docker for deployment infra", "no-proj-sess", "alice")

    result = {
        "evaluations": [
            {
                "learning_id": lid,
                "verdict": "promote",
                "fact": "Docker is used for deployment infrastructure here",
                "category": "project",
            },
        ]
    }
    await _apply_curator_result(db, "no-proj-sess", result)
    facts = await get_facts(db, is_admin=True)
    docker_facts = [f for f in facts if "Docker is used" in f["content"]]
    assert len(docker_facts) == 1
    assert docker_facts[0]["project_id"] is None
