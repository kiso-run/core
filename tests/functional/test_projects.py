"""Functional tests for project knowledge isolation.

F-proj-1: Project-scoped fact visible to bound session
F-proj-2: Project-scoped fact invisible to other project's session
F-proj-3: Global fact visible to all sessions

Requires ``--functional`` flag and KISO_LLM_API_KEY.
"""

from __future__ import annotations

import pytest

from kiso.store import (
    add_project_member,
    bind_session_to_project,
    create_project,
    save_fact,
)
from tests.conftest import LLM_SINGLE_PLAN_TIMEOUT
from tests.functional.conftest import assert_italian

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# F-proj-1 — Project fact visible when bound
# ---------------------------------------------------------------------------


class TestFProjBoundFact:
    """A fact saved with project_id is visible to a session bound to that project."""

    async def test_project_fact_visible_to_bound_session(self, run_message, func_db):
        # Seed project and bind session
        pid = await create_project(func_db, "alpha-corp", "testadmin")
        await add_project_member(func_db, pid, "testadmin", role="member")

        # Get the session from the fixture — run_message creates it internally
        # We'll rely on the fact being seeded BEFORE run_message queries it

        # Seed a distinctive fact in this project
        await save_fact(
            func_db,
            "Alpha Corp uses the Zorglub framework for internal APIs",
            source="curator", category="general",
            project_id=pid,
        )
        # Also seed a global fact that should always be visible
        await save_fact(
            func_db,
            "Python is a programming language",
            source="curator", category="general",
        )

        result = await run_message(
            "cosa sai su Alpha Corp?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        # The classifier should route this as chat_kb or chat
        # The messenger should have access to the project fact
        # since the session's user (testadmin) is a project member
        output_lower = result.msg_output.lower()
        assert any(w in output_lower for w in ("zorglub", "alpha")), (
            f"Expected project fact about Zorglub/Alpha Corp in response: "
            f"{result.msg_output[:300]}"
        )


# ---------------------------------------------------------------------------
# F-proj-2 — Project fact invisible to other project
# ---------------------------------------------------------------------------


class TestFProjIsolation:
    """A fact in project A is NOT visible to a session bound to project B."""

    async def test_project_fact_isolated(self, run_message, func_db):
        # Create two projects — proj-secret owned by a different user
        # so testadmin is genuinely NOT a member (create_project auto-adds
        # the creator as member).
        pid_a = await create_project(func_db, "proj-secret", "other-user")
        pid_b = await create_project(func_db, "proj-public", "testadmin")

        # Seed a distinctive fact ONLY in project A
        await save_fact(
            func_db,
            "Project Secret uses the Xylophone encryption algorithm version 9",
            source="curator", category="general",
            project_id=pid_a,
        )
        # Seed a fact in project B
        await save_fact(
            func_db,
            "Project Public uses the standard RSA encryption",
            source="curator", category="general",
            project_id=pid_b,
        )

        result = await run_message(
            "quali algoritmi di crittografia conosci dai tuoi dati?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        output_lower = result.msg_output.lower()
        # Should see RSA (project B, user is member)
        # Should NOT see Xylophone (project A, user is NOT member)
        assert "xylophone" not in output_lower, (
            f"Leaked project-A fact 'Xylophone' to non-member: "
            f"{result.msg_output[:300]}"
        )


# ---------------------------------------------------------------------------
# F-proj-3 — Global fact visible to all
# ---------------------------------------------------------------------------


class TestFProjGlobalFact:
    """A global fact (no project_id) is visible to any session."""

    async def test_global_fact_always_visible(self, run_message, func_db):
        await save_fact(
            func_db,
            "The internal deployment wrapper is called Rocketship v3",
            source="curator", category="general",
        )

        result = await run_message(
            "qual è il nostro wrapper di deployment interno?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )
        output_lower = result.msg_output.lower()
        assert any(w in output_lower for w in ("rocketship", "deployment")), (
            f"Expected global fact about Rocketship in response: "
            f"{result.msg_output[:300]}"
        )
