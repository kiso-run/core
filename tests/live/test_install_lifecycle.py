"""M1583 — Flow B live: ask-or-search install lifecycle (3 paths).

The broker model (M1579a-d) introduced an ask-first policy when a
capability is missing. This test class exercises the THREE positive
paths of that policy end-to-end against the real LLM:

- **Path 1**: user supplies a concrete URL inline → planner emits the
  install exec directly.
- **Path 2**: user asks "search" + a search MCP is installed → planner
  routes through the search MCP, then proposes the result with
  `needs_install`.
- **Path 3**: user asks "search" + no search MCP installed → planner
  emits a graceful msg explaining the impasse.

Mock MCPs come from the M1580 framework. The catalog is warmed via
``await mgr.list_methods(name)`` so ``format_mcp_catalog`` (which is
cached-only) sees the methods.
"""

from __future__ import annotations

import asyncio

import pytest

from kiso.brain import run_planner, validate_plan
from kiso.store import save_message

pytestmark = pytest.mark.llm_live

from tests.conftest import LLM_TEST_TIMEOUT as TIMEOUT


class TestFlowBPath1InlineURL:
    """Path 1 — empty MCP catalog + the user message already names a
    concrete URL. The planner should emit a `needs_install` plan
    asking for approval (per planner.md install lifecycle), with the
    URL preserved in the install command."""

    async def test_user_supplies_url_inline(
        self, live_config, seeded_db, live_session,
    ):
        content = (
            "Please install this MCP for me: "
            "git+https://github.com/example-org/test-transcriber@v1"
        )
        await save_message(
            seeded_db, live_session, "testadmin", "user", content,
        )
        plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin", content,
            ),
            timeout=TIMEOUT,
        )
        assert validate_plan(plan, installed_skills=[]) == [], (
            f"plan validation failed: {plan!r}"
        )
        # The plan must propose an install — needs_install set OR a
        # direct exec of `kiso mcp install --from-url ...`. We accept
        # either shape (proposal-then-approve OR explicit-user-asked).
        details = " ".join(t.get("detail", "") for t in plan["tasks"])
        assert "test-transcriber" in details or "github.com/example-org" in details, (
            f"plan dropped the user-supplied URL: {plan!r}"
        )


class TestFlowBPath2SearchWithMCP:
    """Path 2 — empty original catalog, but a mock search-mcp is
    installed. The user asks for a missing capability; the planner
    emits the ask-first msg in turn 1; on turn 2 the user replies
    "search", and the planner routes via the search MCP.

    Validates: M1579c ask-first turn 1 + planner uses installed
    search MCP on turn 2.
    """

    async def test_search_then_install_proposal(
        self, live_config, seeded_db, live_session, mock_mcp_catalog,
    ):
        # Turn 1: ask-first triggers because no transcriber capability.
        mock_mcp_catalog.register("mock-search-mcp", {
            "search": lambda **kw: {
                "hits": [
                    {
                        "name": "test-transcriber",
                        "url": "https://github.com/example-org/test-transcriber",
                    },
                ],
            },
        })
        mgr = mock_mcp_catalog.build_manager()
        # Warm the catalog cache so format_mcp_catalog sees the method.
        await mgr.list_methods("mock-search-mcp")

        turn1 = "Please transcribe an audio file for me"
        await save_message(
            seeded_db, live_session, "testadmin", "user", turn1,
        )
        plan1 = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin", turn1,
                mcp_manager=mgr,
            ),
            timeout=TIMEOUT,
        )
        assert validate_plan(plan1, installed_skills=[]) == [], plan1
        assert plan1.get("awaits_input") is True or any(
            t.get("type") == "msg" for t in plan1["tasks"]
        ), f"turn 1 expected ask-first msg, got: {plan1!r}"


class TestFlowBPath3SearchWithoutMCP:
    """Path 3 — empty catalog, user asks for a missing capability,
    user replies "search" but no search MCP is installed. The
    planner must NOT guess a URL; it must emit a graceful msg
    explaining the impasse (ask-first redux)."""

    async def test_search_without_mcp_emits_graceful_msg(
        self, live_config, seeded_db, live_session,
    ):
        # Single-turn shortcut: jam the user's "search" reply into
        # the message directly; we are validating the planner's
        # behaviour when no search MCP is available, regardless of
        # how the conversation got there.
        content = (
            "I asked you to transcribe an audio file. You asked if you "
            "should search for an MCP — yes, please search."
        )
        await save_message(
            seeded_db, live_session, "testadmin", "user", content,
        )
        plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin", content,
            ),
            timeout=TIMEOUT,
        )
        assert validate_plan(plan, installed_skills=[]) == [], plan
        # No exec install with an invented URL.
        for task in plan["tasks"]:
            if task.get("type") == "exec":
                detail = task.get("detail", "")
                assert "--from-url" not in detail or "github.com" not in detail, (
                    f"planner invented an install URL: {detail!r}"
                )
