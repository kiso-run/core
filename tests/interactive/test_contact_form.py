"""Interactive test — contact form with CAPTCHA.

Exercises the full human-relay loop:
1. Agent navigates to guidance.studio
2. Agent fills in form fields
3. Agent hits CAPTCHA → takes screenshot → asks human to solve
4. Human types CAPTCHA solution
5. Agent enters solution and submits
6. Test verifies success

Requires:
- Docker (functional test environment) with browser tool
- Human at terminal
- ``--interactive --functional`` flags

Run: uv run pytest tests/interactive/test_contact_form.py -v --interactive --functional
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.interactive, pytest.mark.functional]


async def test_contact_form_with_captcha(human_relay):
    """Submit guidance.studio contact form — human solves CAPTCHA."""
    result = await human_relay.send(
        "vai su guidance.studio, compila il form di contatto con: "
        "nome 'Kiso Test', email 'test@kiso.run', "
        "messaggio 'Test automatico da kiso interactive suite'. "
        "Se c'è un captcha, fammi uno screenshot e chiedimi di risolverlo."
    )
    assert result.success
