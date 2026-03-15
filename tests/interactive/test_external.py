"""M625a: Interactive test — external actions (SSH key deploy).

Tests flows where kiso asks the user to perform an action on an
external service (GitHub, GitLab, etc.).

The HumanRelay handles the pause:
1. Kiso generates SSH key, shows it in msg
2. HumanRelay prints key to terminal
3. Human adds key on GitHub
4. HumanRelay sends "done"
5. Kiso verifies with ssh -T git@github.com

Requires:
- Docker (functional test environment)
- Human at terminal with GitHub/GitLab access
- ``--interactive --functional`` flags

Run: uv run pytest tests/interactive/test_external.py -v --interactive --functional
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.interactive, pytest.mark.functional]


async def test_ssh_key_deploy_github(human_relay):
    """Kiso generates SSH key, user adds it to GitHub, kiso verifies."""
    result = await human_relay.send(
        "genera una chiave SSH e verifica che funzioni con GitHub"
    )
    assert result.success


async def test_ssh_key_deploy_gitlab(human_relay):
    """Same flow for GitLab."""
    result = await human_relay.send(
        "genera una chiave SSH e verifica che funzioni con GitLab"
    )
    assert result.success
