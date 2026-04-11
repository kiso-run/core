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
    search_facts_scored,
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


async def test_fact_admin_no_session_sees_all(db):
    """Admin with session=None (system queries) sees everything.

    This is the consolidator/dedup path — no session context means
    cross-project visibility is needed.
    """
    pid = await create_project(db, "proj-secret", "alice")
    await save_fact(db, "Secret admin-only project fact", "curator",
                    category="project", project_id=pid)
    await save_fact(db, "User session-only fact content here", "curator",
                    category="user", session="sess1")
    facts = await get_facts(db, is_admin=True)  # session=None (default)
    assert any("Secret admin-only" in f["content"] for f in facts)
    assert any("User session-only" in f["content"] for f in facts)


async def test_fact_admin_with_session_excludes_project_facts(db):
    """M1305: Admin with an active session but no project binding must
    NOT see project-scoped facts.  Admin privilege bypasses session scoping
    (can see user-category from any session), not project scoping.
    """
    pid = await create_project(db, "proj-secret", "alice")
    await save_fact(db, "Secret project fact for isolation", "curator",
                    category="project", project_id=pid)
    await save_fact(db, "User session-only fact content here", "curator",
                    category="user", session="sess1")
    await save_fact(db, "Global fact visible to all", "curator",
                    category="general")
    # Admin WITH session but WITHOUT project binding
    facts = await get_facts(db, session="sess-admin", is_admin=True)
    assert any("Global fact" in f["content"] for f in facts)
    assert any("User session-only" in f["content"] for f in facts)
    assert not any("Secret project fact" in f["content"] for f in facts), (
        "Admin with session but no project context must NOT see project-scoped facts"
    )


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


async def test_fact_session_only_filter(db):
    """Without username, session-only filter applies (user category filtered by session)."""
    await save_fact(db, "User-scoped fact in session", "curator",
                    category="user", session="sess1")
    await save_fact(db, "Global general fact content", "curator", category="general")
    facts = await get_facts(db, session="sess1")
    assert any("User-scoped" in f["content"] for f in facts)
    assert any("Global general" in f["content"] for f in facts)
    # Different session — user fact not visible
    facts = await get_facts(db, session="sess2")
    assert not any("User-scoped" in f["content"] for f in facts)


# --- API access control tests ---


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


@pytest.mark.parametrize("args_list,expected", [
    (["project", "list"], {"command": "project", "project_cmd": "list"}),
    (["project", "create", "my-app", "--description", "A test project"],
     {"name": "my-app", "description": "A test project"}),
    (["project", "bind", "sess1", "my-app"],
     {"session": "sess1", "project": "my-app"}),
    (["project", "add-member", "bob", "--project", "my-app", "--role", "viewer"],
     {"username": "bob", "project": "my-app", "role": "viewer"}),
    (["project", "members", "--project", "my-app"],
     {"project": "my-app"}),
])
def test_cli_project_parser(args_list, expected):
    """Verify project CLI subcommand parsers accept the right arguments."""
    from cli import build_parser
    parser = build_parser()
    args = parser.parse_args(args_list)
    for attr, value in expected.items():
        assert getattr(args, attr) == value


# --- M689: Curator project-awareness ---


@pytest.mark.parametrize("category,has_project,expected_project_id_set,extra_checks", [
    # project category + project session → project_id set
    ("project", True, True, {}),
    # general category + project session → global (no project_id)
    ("general", True, False, {}),
    # behavior category + project session → project_id set
    ("behavior", True, True, {}),
    # project category + no project session → global
    ("project", False, False, {}),
    # user category + project session → no project_id, but session set
    ("user", True, False, {"check_session": True}),
])
async def test_curator_project_scoping(db, category, has_project, expected_project_id_set, extra_checks):
    """Curator assigns project_id based on category and session binding."""
    from kiso.worker.loop import _apply_curator_result
    from kiso.store import save_learning

    suffix = f"{category}-{has_project}"
    sess = f"cur-sess-{suffix}"
    await create_session(db, sess)

    if has_project:
        pid = await create_project(db, f"curator-proj-{suffix}", "alice")
        await bind_session_to_project(db, sess, pid)
    else:
        pid = None

    learning_text = f"Learning about {suffix} for curator scoping test"
    lid = await save_learning(db, learning_text, sess, "alice")

    fact_text = f"Promoted fact for {suffix} curator scoping test"
    evaluation = {
        "learning_id": lid,
        "verdict": "promote",
        "fact": fact_text,
        "category": category,
    }
    if category in ("project", "user"):
        evaluation["tags"] = ["test-tag"]

    await _apply_curator_result(db, sess, {"evaluations": [evaluation]})

    # Pass project_id so admin can see project-scoped facts (M1305:
    # admin without project context no longer bypasses project filter).
    facts = await get_facts(db, is_admin=True, project_id=pid)
    matched = [f for f in facts if fact_text in f["content"]]
    assert len(matched) == 1

    if expected_project_id_set:
        assert matched[0]["project_id"] == pid
    else:
        assert matched[0]["project_id"] is None

    if extra_checks.get("check_session"):
        assert matched[0]["session"] == sess


# --- M1257: project_id-based filtering (no-username path) ---


async def test_get_facts_no_username_no_project_id_excludes_project_facts(db):
    """When neither username nor project_id is provided (and not admin),
    project-scoped facts MUST NOT leak. Only global facts (project_id IS NULL)
    are returned (modulo the user-category session rule).
    """
    pid = await create_project(db, "leaky-proj", "alice")
    await save_fact(db, "Project secret xylophone version 9", "curator",
                    category="project", project_id=pid)
    await save_fact(db, "Global fact about rocketships", "curator", category="general")

    facts = await get_facts(db, session="sess1")
    contents = [f["content"] for f in facts]
    assert any("rocketships" in c for c in contents)
    assert not any("xylophone" in c for c in contents), \
        "Project-scoped fact must not leak when no username and no project_id"


async def test_get_facts_with_project_id_includes_that_project(db):
    """When project_id is passed (session bound to project A), facts of that
    project AND global facts are returned, but other-project facts are NOT.
    """
    pid_a = await create_project(db, "proj-a", "alice")
    pid_b = await create_project(db, "proj-b", "alice")
    await save_fact(db, "Alpha proj fact apple", "curator",
                    category="project", project_id=pid_a)
    await save_fact(db, "Bravo proj fact banana", "curator",
                    category="project", project_id=pid_b)
    await save_fact(db, "Global cherry", "curator", category="general")

    facts = await get_facts(db, session="sess1", project_id=pid_a)
    contents = [f["content"] for f in facts]
    assert any("apple" in c for c in contents)
    assert any("cherry" in c for c in contents)
    assert not any("banana" in c for c in contents)


async def test_search_facts_scored_no_username_no_project_id_excludes_project_facts(db):
    """search_facts_scored with explicit is_admin=False, session, no username,
    no project_id MUST NOT return project-scoped facts.
    """
    pid = await create_project(db, "scored-leak", "alice")
    await save_fact(db, "Xylophone leaked from scored search", "curator",
                    category="project", project_id=pid, tags=["leakage"])
    await save_fact(db, "Global keyword fact xylophone available", "curator",
                    category="general", tags=["leakage"])

    results = await search_facts_scored(
        db, tags=["leakage"], session="sess1", is_admin=False,
    )
    contents = [r["content"] for r in results]
    assert any("Global keyword fact" in c for c in contents)
    assert not any("leaked from scored" in c for c in contents)


async def test_search_facts_scored_with_project_id_filters_other_projects(db):
    """search_facts_scored with project_id includes that project + global only."""
    pid_a = await create_project(db, "scored-a", "alice")
    pid_b = await create_project(db, "scored-b", "alice")
    await save_fact(db, "Apple fact in scored a", "curator",
                    category="project", project_id=pid_a, tags=["t"])
    await save_fact(db, "Banana fact in scored b", "curator",
                    category="project", project_id=pid_b, tags=["t"])
    await save_fact(db, "Global cherry scored fact", "curator",
                    category="general", tags=["t"])

    results = await search_facts_scored(
        db, tags=["t"], session="sess1", is_admin=False, project_id=pid_a,
    )
    contents = [r["content"] for r in results]
    assert any("Apple fact" in c for c in contents)
    assert any("cherry" in c for c in contents)
    assert not any("Banana fact" in c for c in contents)


async def test_search_facts_scored_admin_with_project_id(db):
    """Admin with explicit project_id sees that project's facts."""
    pid = await create_project(db, "admin-default", "alice")
    await save_fact(db, "Project admin-default fact zeta", "curator",
                    category="project", project_id=pid, tags=["k"])
    results = await search_facts_scored(db, tags=["k"], project_id=pid)
    assert any("zeta" in r["content"] for r in results)


# --- M1305: is_admin must NOT bypass project isolation ---


async def test_is_admin_does_not_bypass_project_isolation_scored(db):
    """M1305: is_admin=True must still respect project_id filtering.

    Admin privilege bypasses *session* scoping (see user-category facts
    from any session).  It must NOT bypass *project* scoping — an admin
    whose session is bound to project A must not see facts from project B.
    """
    pid_a = await create_project(db, "admin-iso-a", "alice")
    pid_b = await create_project(db, "admin-iso-b", "bob")
    await save_fact(db, "Secret recipe in project A", "curator",
                    category="general", project_id=pid_a, tags=["food"])
    await save_fact(db, "Public recipe in project B", "curator",
                    category="general", project_id=pid_b, tags=["food"])
    await save_fact(db, "Global cooking tip", "curator",
                    category="general", tags=["food"])

    results = await search_facts_scored(
        db, tags=["food"], session="sess1", is_admin=True, project_id=pid_a,
    )
    contents = [r["content"] for r in results]
    assert any("Secret recipe" in c for c in contents), "own-project fact must be visible"
    assert any("Global cooking" in c for c in contents), "global fact must be visible"
    assert not any("Public recipe" in c for c in contents), (
        "is_admin=True must NOT leak facts from other projects"
    )


async def test_is_admin_does_not_bypass_project_isolation_get_facts(db):
    """M1305: get_facts with is_admin=True must still filter by project_id."""
    pid_a = await create_project(db, "admin-getf-a", "alice")
    pid_b = await create_project(db, "admin-getf-b", "bob")
    await save_fact(db, "Alpha secret in A", "curator",
                    category="general", project_id=pid_a)
    await save_fact(db, "Bravo secret in B", "curator",
                    category="general", project_id=pid_b)
    await save_fact(db, "Global gamma fact", "curator",
                    category="general")

    facts = await get_facts(db, session="sess1", is_admin=True, project_id=pid_a)
    contents = [f["content"] for f in facts]
    assert any("Alpha secret" in c for c in contents)
    assert any("Global gamma" in c for c in contents)
    assert not any("Bravo secret" in c for c in contents), (
        "is_admin=True must NOT leak facts from other projects"
    )


async def test_is_admin_does_not_bypass_project_isolation_search_facts(db):
    """M1305: search_facts with is_admin=True + project_id must filter."""
    pid_a = await create_project(db, "admin-sf-a", "alice")
    pid_b = await create_project(db, "admin-sf-b", "bob")
    await save_fact(db, "Xylophone encryption in project A", "curator",
                    category="general", project_id=pid_a)
    await save_fact(db, "Xylophone mention in project B", "curator",
                    category="general", project_id=pid_b)
    await save_fact(db, "Global xylophone fact", "curator",
                    category="general")

    results = await search_facts(
        db, "xylophone", session="sess1", is_admin=True, project_id=pid_a,
    )
    contents = [r["content"] for r in results]
    assert any("project A" in c for c in contents)
    assert any("Global xylophone" in c for c in contents)
    assert not any("project B" in c for c in contents), (
        "is_admin=True must NOT leak facts from other projects"
    )
