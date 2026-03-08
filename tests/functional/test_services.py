"""F5-F6: External service interaction functional tests.

F5: Sign up for moltbook.
F6: Write a post on moltbook.

Both tests are destructive (real side effects on external services).
F6 depends on F5 — they share a session so F6 has conversation context
from the signup.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
    assert_url_reachable,
)

pytestmark = [pytest.mark.functional, pytest.mark.destructive]

SERVICE_TIMEOUT = 300  # browser navigation + form filling


# ---------------------------------------------------------------------------
# F5 — Service signup (moltbook)
# ---------------------------------------------------------------------------


class TestF5MoltbookSignup:
    """Sign up for moltbook via browser skill."""

    async def test_service_signup(self, run_message):
        result = await run_message(
            "iscriviti a moltbook",
            timeout=SERVICE_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian
        assert_italian(result.msg_output)
        assert_no_failure_language(result.msg_output)

        # Browser skill was used
        skill_names = [t.get("skill") for t in result.skill_tasks()]
        assert "browser" in skill_names, (
            f"Browser skill not used. Skills: {skill_names}"
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
    """Write a post on moltbook (requires prior signup in same session)."""

    async def test_service_post(self, run_message):
        # First ensure signup happened (sends in same session)
        signup_result = await run_message(
            "iscriviti a moltbook",
            timeout=SERVICE_TIMEOUT,
        )
        assert signup_result.success, (
            f"Signup failed — cannot test posting. "
            f"Plans: {[p.get('status') for p in signup_result.plans]}"
        )

        # Now post
        result = await run_message(
            "scrivi un post su moltbook sul fatto che stai facendo il tuo "
            "primo post di test sulla piattaforma",
            timeout=SERVICE_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian
        assert_italian(result.msg_output)
        assert_no_failure_language(result.msg_output)

        # Browser skill was used
        skill_names = [t.get("skill") for t in result.skill_tasks()]
        assert "browser" in skill_names, (
            f"Browser skill not used. Skills: {skill_names}"
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
            await assert_url_reachable(pf["url"])
