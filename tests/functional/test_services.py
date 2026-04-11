"""F5-F6: External service interaction functional tests.

F5: Sign up for moltbook.
F6: Write a post on moltbook.

Both tests are destructive (real side effects on external services).
F6 depends on F5 — they share a session so F6 has conversation context
from the signup.

These are best-effort external smoke tests, not blocking semantic coverage.
The remote site controls the final oracle, so they run only with
`--functional --destructive --extended`.
"""

from __future__ import annotations

import pytest

from tests.conftest import LLM_REPLAN_TIMEOUT
from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
    assert_url_reachable,
)

pytestmark = [
    pytest.mark.functional,
    pytest.mark.destructive,
    pytest.mark.extended,
]


# ---------------------------------------------------------------------------
# F5 — Service signup (moltbook)
# ---------------------------------------------------------------------------


class TestF5MoltbookSignup:
    """Best-effort smoke test for moltbook signup via browser skill."""

    async def test_service_signup(self, run_message):
        """What: External service signup test via browser tool on moltbook.

        Why: Validates real-world browser interaction -- form filling and account
        creation. This is the highest-stakes browser test as it performs destructive
        side effects on an external service.
        Expects: Plan succeeds, Italian response, browser tool used, output contains
        signup confirmation keywords (iscri/registr/account/etc.).
        """
        result = await run_message(
            "iscriviti a moltbook",
            timeout=LLM_REPLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian
        assert_italian(result.last_plan_msg_output)
        assert_no_failure_language(result.last_plan_msg_output)

        # Browser skill was used
        wrapper_names = [t.get("skill") for t in result.tool_tasks()]
        assert "browser" in wrapper_names, (
            f"Browser skill not used. Skills: {wrapper_names}"
        )

        # Output mentions successful registration
        lower = result.msg_output.lower()
        signup_keywords = (
            "iscri", "registr", "account", "profilo",
            "creato", "completat", "benvenuto",
        )
        assert any(kw in lower for kw in signup_keywords), (
            f"No signup confirmation in output: {result.msg_output[:400]}"
        )


# ---------------------------------------------------------------------------
# F6 — Service posting (moltbook)
# ---------------------------------------------------------------------------


class TestF6MoltbookPost:
    """Best-effort smoke test for posting after signup in the same session."""

    async def test_service_post(self, run_message, func_app_client):
        """What: Multi-step service interaction test: signup then post on moltbook.

        Why: Validates session continuity across turns -- the signup context must
        carry over so the agent can perform an authenticated action (posting).
        Without session continuity, multi-step service workflows would fail.
        Expects: Signup succeeds, then posting succeeds with Italian response,
        browser tool used, output mentions posting keywords.
        """
        # First ensure signup happened (sends in same session)
        signup_result = await run_message(
            "iscriviti a moltbook",
            timeout=LLM_REPLAN_TIMEOUT,
        )
        assert signup_result.success, (
            f"Signup failed — cannot test posting. "
            f"Plans: {[p.get('status') for p in signup_result.plans]}"
        )

        # Now post
        result = await run_message(
            "scrivi un post su moltbook sul fatto che stai facendo il tuo "
            "primo post di test sulla piattaforma",
            timeout=LLM_REPLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian
        assert_italian(result.last_plan_msg_output)
        assert_no_failure_language(result.last_plan_msg_output)

        # Browser skill was used
        wrapper_names = [t.get("skill") for t in result.tool_tasks()]
        assert "browser" in wrapper_names, (
            f"Browser skill not used. Skills: {wrapper_names}"
        )

        # Output mentions posting
        lower = result.msg_output.lower()
        post_keywords = (
            "post", "pubblic", "scritto", "condivi",
            "messaggio", "contenuto",
        )
        assert any(kw in lower for kw in post_keywords), (
            f"No posting confirmation in output: {result.msg_output[:400]}"
        )

        # If a post URL is in the output, verify it's reachable
        for pf in result.pub_files:
            await assert_url_reachable(pf["url"], client=func_app_client)
