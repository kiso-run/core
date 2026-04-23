"""Concern 11 — chat-mediated install proposals must surface trust info.

When a user interacts with kiso through a connector (Telegram,
Slack, email) the trust gate for ``kiso skill install`` /
``kiso mcp install`` cannot pop an interactive prompt on the
daemon host — the "yes/no" decision arrives as a chat reply.

The planner prompt (``skills_and_mcp`` module) must therefore
instruct the planner, when emitting a ``needs_install`` msg plan,
to include enough context for the user to make the trust decision
in chat:

- the resolved source key (``github.com/owner/repo`` or ``npm:@scope/pkg``),
- the trust tier (tier1 / custom / untrusted),
- any risk factors the install would detect (bundled ``scripts/``,
  wide ``allowed-tools``, oversized assets).

Without this, a remote user sees only "I will install X" and the
trust decision collapses to a name check.
"""

from __future__ import annotations

from pathlib import Path

import pytest


PLANNER_PROMPT = (
    Path(__file__).resolve().parents[2] / "kiso" / "roles" / "planner.md"
)


def test_planner_prompt_requires_trust_surface_in_install_proposal():
    prompt = PLANNER_PROMPT.read_text(encoding="utf-8")
    # Locate the skills_and_mcp module — the install-routing rules.
    marker = "<!-- MODULE: skills_and_mcp -->"
    assert marker in prompt, "skills_and_mcp module must exist"
    module = prompt.split(marker, 1)[1]
    # The module continues until the next module marker.
    next_marker = "<!-- MODULE: "
    if next_marker in module:
        module = module.split(next_marker, 1)[0]

    module_lower = module.lower()
    # The proposal msg must spell out source, trust tier, and risks
    # so a chat user can make an informed approval.
    assert "trust" in module_lower, module
    assert (
        "tier" in module_lower or "untrusted" in module_lower
    ), module
    assert (
        "risk" in module_lower or "scripts/" in module_lower
    ), module
    # And the source itself — not just the user-friendly name.
    assert "source" in module_lower, module
