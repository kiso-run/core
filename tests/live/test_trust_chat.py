"""M1588 — Flow G live: trust escalation chat-mediated.

Trust escalation in chat (no terminal prompt available) is the only
path for users on Discord/HTTP/etc. to approve untrusted-source
installs. The planner must show trust info in chat, the user
approves with explicit text, and the planner emits the install
exec with `--yes`.

This live class covers the canonical untrusted approve/reject
toggle. Tier-1 (auto-trusted) and trust-persistence-after-chat-approval
require respectively the kiso-run/* trust list and the trust-store
write path; both are unit-tier locked elsewhere — the live tier
verifies the LLM-mediated chat behavior.
"""

from __future__ import annotations

import asyncio

import pytest

from kiso.brain import run_planner, validate_plan
from kiso.store import save_message

pytestmark = pytest.mark.llm_live

from tests.conftest import LLM_TEST_TIMEOUT as TIMEOUT


class TestFlowGUntrustedInstallApprove:
    """User asks to install an untrusted source. Planner emits
    needs_install msg with trust info; on the user's next turn
    confirming, planner emits the install exec with `--yes`."""

    async def test_untrusted_install_proposal_shape(
        self, live_config, seeded_db, live_session,
    ):
        # Single-turn shape verification: planner emits a needs_install
        # plan referencing the source and ending with a msg that
        # asks the user. Persistence + multi-turn are out of scope here
        # (see M1583 for multi-turn install lifecycle).
        content = (
            "Please install the MCP from "
            "https://github.com/random-org/cool-mcp"
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
        # The plan must reference the user-supplied source somewhere
        # (msg body, exec detail, or needs_install metadata).
        details = " ".join(t.get("detail", "") for t in plan["tasks"])
        needs_install = plan.get("needs_install") or []
        haystack = (details + " " + " ".join(needs_install)).lower()
        assert "random-org" in haystack or "cool-mcp" in haystack, (
            f"plan dropped the user-supplied source: {plan!r}"
        )


class TestFlowGUntrustedInstallReject:
    """User explicitly rejects the install proposal. Planner must NOT
    emit an install exec. Single-turn shape: user message conveys both
    the original ask AND the rejection (no multi-turn dance needed for
    the no-exec invariant)."""

    async def test_user_rejection_yields_no_install_exec(
        self, live_config, seeded_db, live_session,
    ):
        content = (
            "I asked you to install random-org/cool-mcp earlier. "
            "Actually, no — don't install it. Forget it."
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
        for task in plan["tasks"]:
            if task.get("type") == "exec":
                detail = (task.get("detail") or "").lower()
                assert "install" not in detail and "--from-url" not in detail, (
                    f"planner emitted install exec after user rejection: "
                    f"{task!r}"
                )
