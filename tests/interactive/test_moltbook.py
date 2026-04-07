"""Interactive test — Moltbook signup and posting.

Requires:
- Docker (functional test environment)
- Human at terminal (to complete tweet verification)
- ``--interactive --functional`` flags

Run: uv run pytest tests/interactive/test_moltbook.py -v --interactive --functional

This file is manual acceptance coverage only. It is not intended to be part of
blocking automated confidence for browser/service workflows.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.interactive,
    pytest.mark.functional,
    pytest.mark.extended,
]


async def test_moltbook_signup(human_relay):
    """Moltbook signup — requires human to post verification tweet."""
    result = await human_relay.send("iscriviti a moltbook")
    assert result.success
    # Agent should confirm signup completed
    msg = (result.msg_output or "").lower()
    assert any(w in msg for w in ("iscritto", "registrat", "signup", "account"))


async def test_moltbook_post(human_relay):
    """Post to Moltbook — may require CAPTCHA or auth."""
    result = await human_relay.send(
        "scrivi un post su moltbook dicendo che kiso funziona bene"
    )
    assert result.success
