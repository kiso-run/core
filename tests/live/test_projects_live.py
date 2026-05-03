"""M1587 — Flow P live: cross-session project knowledge sharing.

A fact taught in session A (bound to project X) must be retrievable
from session B when B is also bound to project X. The unit + DB tier
already verifies the storage path; this live test confirms the
project-scoped visibility invariant under live conditions.

(M1620: the classifier-routing assertion was removed when the
classifier was retired. The planner's Decision Tree branch 5
handles the kb_answer routing now and is exercised by the
broker-generalist live tests; the storage / cross-session
visibility invariant pinned here remains independent of routing.)
"""

from __future__ import annotations

import uuid

import pytest

from kiso.store import (
    create_project,
    create_session,
    save_fact,
    save_message,
    bind_session_to_project,
    get_session_project_id,
    search_facts,
)

pytestmark = pytest.mark.llm_live


class TestFlowPCrossSessionProject:
    """Two sessions on the same project share knowledge."""

    async def test_fact_taught_in_session_a_visible_in_session_b(
        self, live_config, live_db,
    ):
        # Setup: project + 2 sessions both bound to it.
        project_name = f"test-proj-{uuid.uuid4().hex[:8]}"
        project_id = await create_project(live_db, project_name, created_by="testadmin")
        session_a = f"sess-a-{uuid.uuid4().hex[:8]}"
        session_b = f"sess-b-{uuid.uuid4().hex[:8]}"
        await create_session(live_db, session_a)
        await create_session(live_db, session_b)
        await bind_session_to_project(live_db, session_a, project_id)
        await bind_session_to_project(live_db, session_b, project_id)

        # Persist a project-scoped fact in session A.
        await save_fact(
            live_db,
            "Our deploy script lives at /opt/deploy.sh",
            source="user", session=session_a, category="project",
            project_id=project_id,
        )

        # The fact must be retrievable from session B's perspective via
        # the same `search_facts` path the briefer uses.
        b_pid = await get_session_project_id(live_db, session_b)
        assert b_pid == project_id

        hits = await search_facts(
            live_db, query="deploy script",
            session=session_b, is_admin=False, project_id=b_pid,
        )
        assert hits, (
            "session B did not see the project-scoped fact taught in A"
        )
        deploy_paths = [
            f.get("content", "") for f in hits
            if "/opt/deploy.sh" in f.get("content", "")
        ]
        assert deploy_paths, (
            f"deploy fact not surfaced in session B: {hits!r}"
        )

        # Persist a user message in session B for completeness — the
        # planner Decision Tree (branch 5: kb_answer) is exercised by
        # the broker-generalist live tier; this test pins only the
        # storage / cross-session visibility invariant.
        await save_message(
            live_db, session_b, "testadmin", "user",
            "where's our deploy script?",
        )
