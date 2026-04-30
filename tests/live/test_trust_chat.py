"""M1588 / M1602 — Flow G live: trust escalation chat-mediated.

Trust escalation in chat (no terminal prompt available) is the only
path for users on Discord/HTTP/etc. to approve untrusted-source
installs. The planner must show trust info in chat, the user
approves with explicit text, and the planner emits the install
exec with `--yes`.

This live class covers the canonical untrusted approve/reject
toggle. Tier-1 (auto-trusted) is unit-tier locked in
`tests/test_trust.py::TestMcpTrust`. Trust persistence after chat
approval is covered by `TestFlowGTrustPersistence` below (M1602).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from kiso.brain import run_planner, validate_plan
from kiso.mcp import trust as mcp_trust
from kiso.store import save_message
from kiso.trust_store import add_prefix

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


_PERSISTENCE_SOURCE_URL = "https://github.com/random-org/cool-mcp"
_PERSISTENCE_SOURCE_KEY = "github.com/random-org/cool-mcp"


def _plan_haystack(plan: dict) -> str:
    """Concatenate every text field a planner could surface trust info in."""
    pieces: list[str] = []
    for task in plan.get("tasks") or []:
        pieces.append(task.get("detail") or "")
        args = task.get("args")
        if isinstance(args, dict):
            for v in args.values():
                if isinstance(v, str):
                    pieces.append(v)
    pieces.extend(plan.get("needs_install") or [])
    return " ".join(pieces).lower()


class TestFlowGTrustPersistence:
    """M1602 — chat-approved install of an untrusted source must lift the
    source into ``tier=custom`` for subsequent install requests.

    Three turns:

    1. User asks to install ``random-org/cool-mcp`` from a github URL.
       The planner emits a `needs_install` proposal that surfaces the
       trust tier as ``untrusted`` (and risk factors).
    2. User replies "yes install it". The planner emits an exec install.
       Once the install runs, the source key must be recorded in the
       user trust store so subsequent installs see ``tier=custom``.
    3. User asks to install the same source again. The proposal must NOT
       re-surface ``untrusted`` / risk factors; ``is_trusted`` returns
       ``custom`` for the source key.

    The trust-store side-effect (turn 2 → turn 3 transition) is
    asserted directly against ``kiso.trust_store`` rather than through
    messenger output, so the test stays robust to phrasing changes.
    """

    async def test_second_install_sees_custom_tier(
        self, live_config, seeded_db, live_session, tmp_path,
    ):
        """KISO_DIR is patched to *tmp_path* so trust.json is isolated.

        The test exercises the planner across three turns and uses
        ``kiso.trust_store.add_prefix`` to model the install-side
        trust-record write that M1588 deferred to this milestone.
        """
        trust_path = tmp_path / "trust.json"
        with patch("kiso.trust_store.TRUST_PATH", trust_path):
            assert mcp_trust.is_trusted(_PERSISTENCE_SOURCE_KEY) == "untrusted"

            # ---------- Turn 1 — initial proposal ----------
            content_1 = (
                f"Please install the MCP from {_PERSISTENCE_SOURCE_URL}"
            )
            await save_message(
                seeded_db, live_session, "testadmin", "user", content_1,
            )
            plan_1 = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin", content_1,
                ),
                timeout=TIMEOUT,
            )
            assert validate_plan(plan_1, installed_skills=[]) == [], plan_1
            haystack_1 = _plan_haystack(plan_1)
            assert "random-org" in haystack_1 or "cool-mcp" in haystack_1, (
                f"turn 1 lost the source: {plan_1!r}"
            )
            assert "untrusted" in haystack_1, (
                "turn 1 must surface tier=untrusted on first install of an "
                f"unknown source: {plan_1!r}"
            )

            # ---------- Turn 2 — user approval triggers install ----------
            content_2 = "yes install it"
            await save_message(
                seeded_db, live_session, "testadmin", "user", content_2,
            )
            plan_2 = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin", content_2,
                    install_approved=True,
                ),
                timeout=TIMEOUT,
            )
            assert validate_plan(plan_2, installed_skills=[]) == [], plan_2
            install_execs = [
                t for t in plan_2["tasks"]
                if t.get("type") == "exec"
                and "--from-url" in (t.get("detail") or "")
            ]
            assert install_execs, (
                f"turn 2 must emit an install exec after approval: {plan_2!r}"
            )

            # Stand in for the worker exec that this live test never
            # actually runs: the planner emits the install command in
            # Turn 2, but only `cli/mcp.py::_cmd_install` writes the
            # prefix when the install really fires (M1604). Calling
            # `add_prefix` here mirrors what M1604's auto-record
            # behaviour would do once the planned exec executes.
            add_prefix("mcp", _PERSISTENCE_SOURCE_KEY)
            assert mcp_trust.is_trusted(_PERSISTENCE_SOURCE_KEY) == "custom"

            # ---------- Turn 3 — second install, tier should be custom ----
            content_3 = (
                "Please install the MCP from "
                f"{_PERSISTENCE_SOURCE_URL} again"
            )
            await save_message(
                seeded_db, live_session, "testadmin", "user", content_3,
            )
            plan_3 = await asyncio.wait_for(
                run_planner(
                    seeded_db, live_config, live_session, "admin", content_3,
                ),
                timeout=TIMEOUT,
            )
            assert validate_plan(plan_3, installed_skills=[]) == [], plan_3
            haystack_3 = _plan_haystack(plan_3)
            assert "untrusted" not in haystack_3, (
                "turn 3 must NOT show tier=untrusted after the source has "
                f"been added to the trust store: {plan_3!r}"
            )
