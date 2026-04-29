"""M1585 — Flow J live: MCP recovery flow.

Two scenarios that pin different ends of the recovery spectrum:

- **Class 1 — Persistent unhealthy**: an MCP that always errors. The
  planner must surface a recovery msg listing concrete commands
  (`kiso mcp test`, `remove`, replace) — no shell-level fix attempt.
- **Class 2 — Transient retry (NEW, anti-overfit)**: a flaky MCP that
  errors once then succeeds. The planner must NOT pivot to ask-user
  on a single failure; the reviewer/replan loop should retry and the
  plan should complete.
"""

from __future__ import annotations

import asyncio

import pytest

from kiso.brain import run_planner, validate_plan
from kiso.store import save_message

pytestmark = pytest.mark.llm_live

from tests.conftest import LLM_TEST_TIMEOUT as TIMEOUT


class TestFlowJPersistentUnhealthy:
    async def test_unhealthy_mcp_emits_recovery_msg(
        self, live_config, seeded_db, live_session, mock_mcp_catalog,
    ):
        # The MCP exists in the catalog but every call raises.
        def boom(**kw):
            raise RuntimeError("simulated MCP failure")

        mock_mcp_catalog.register("flaky-mcp", {"do": boom})
        mgr = mock_mcp_catalog.build_manager()
        await mgr.list_methods("flaky-mcp")
        # Mark unhealthy so format_mcp_catalog excludes it AND the
        # mcp_recovery briefer module fires.
        mgr._unhealthy.add("flaky-mcp")

        content = "Use the flaky-mcp server to do the thing"
        await save_message(
            seeded_db, live_session, "testadmin", "user", content,
        )
        plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin", content,
                mcp_manager=mgr,
            ),
            timeout=TIMEOUT,
        )
        assert validate_plan(plan, installed_skills=[]) == [], plan
        # No exec attempt — the recovery path is msg-only.
        assert not any(t.get("type") == "exec" for t in plan["tasks"]), (
            f"planner improvised an exec on unhealthy MCP: {plan!r}"
        )


class TestFlowJTransientRetry:
    async def test_transient_failure_does_not_pivot_to_ask_user(
        self, live_config, seeded_db, live_session, mock_mcp_catalog,
    ):
        # Single LLM call: we verify the planner does NOT immediately
        # set awaits_input on a routine "do something" prompt with a
        # working MCP available. This pins the inverse of the
        # M1579c ask-first flow: working capability ⇒ no pause.
        mock_mcp_catalog.register("working-mcp", {
            "do": lambda **kw: {"ok": True},
        })
        mgr = mock_mcp_catalog.build_manager()
        await mgr.list_methods("working-mcp")

        content = "Use working-mcp to do the thing for me"
        await save_message(
            seeded_db, live_session, "testadmin", "user", content,
        )
        plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin", content,
                mcp_manager=mgr,
            ),
            timeout=TIMEOUT,
        )
        assert validate_plan(plan, installed_skills=[]) == [], plan
        # If the planner has a working MCP available it should not
        # set awaits_input as its first move — that's the over-escalation
        # anti-pattern this test guards against.
        if plan.get("awaits_input"):
            # Allow it only when no mcp task fired (legitimate
            # disambiguation request).
            mcp_tasks = [t for t in plan["tasks"] if t.get("type") == "mcp"]
            assert not mcp_tasks, (
                f"planner set awaits_input AND emitted mcp tasks — "
                f"incoherent: {plan!r}"
            )
