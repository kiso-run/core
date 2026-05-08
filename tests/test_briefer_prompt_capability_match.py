"""Content-lock for M1647.

`kiso/roles/briefer.md` must contain an explicit
"capability-match override" clause that exempts user-named
capabilities from the AGGRESSIVE-filtering default. Without
this, the briefer occasionally prunes the right MCP method
(e.g. `ocr-mcp:extract_text` for an explicit OCR request) and
surfaces an unrelated method carried over from the prior turn,
forcing the planner into exec fallbacks that break the M1609
capability rule downstream.

This file pins the new clause text against accidental removal.
Pattern follows the M1619 / M1646 content-lock convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"


@pytest.fixture(scope="module")
def briefer_text() -> str:
    return (_ROLES_DIR / "briefer.md").read_text()


class TestCapabilityMatchOverride:
    """Briefer must declare the capability-match override as an
    explicit exception to AGGRESSIVE filtering."""

    def test_anchor_phrase_present(self, briefer_text: str):
        """The override is identified by a stable anchor phrase
        ('Capability-match override'). Removing the phrase removes
        the rule's identity — content lock catches that."""
        assert "Capability-match override" in briefer_text, (
            "briefer.md must contain a 'Capability-match override' "
            "clause that exempts user-named capabilities from "
            "AGGRESSIVE filtering. M1647 motivation: the briefer "
            "prunes ocr-mcp:extract_text on explicit OCR requests, "
            "breaking the cross-plan MCP routing test."
        )

    def test_override_marks_aggressive_exception(self, briefer_text: str):
        """The clause must explicitly mark itself as an exception
        to AGGRESSIVE filtering — not a soft preference. Without
        this framing the model treats it as advice, not rule."""
        idx = briefer_text.find("Capability-match override")
        assert idx != -1
        window = briefer_text[idx : idx + 1500]
        assert "AGGRESSIVE" in window or "aggressive" in window, (
            "the override clause must reference AGGRESSIVE filtering "
            "so the model recognizes it as an exception to the default "
            "filtering rule, not a soft suggestion"
        )

    def test_override_is_must_inclusion(self, briefer_text: str):
        """The action verb must be 'MUST' (or equivalent hard
        directive) targeting `mcp_methods`. Soft verbs ('should',
        'consider') let the LLM downgrade the rule."""
        idx = briefer_text.find("Capability-match override")
        window = briefer_text[idx : idx + 1500]
        assert "MUST" in window, (
            "the override clause must use MUST (capitalized) to "
            "make the inclusion rule a hard directive rather than "
            "soft preference"
        )
        assert "mcp_methods" in window, (
            "the override must explicitly target the `mcp_methods` "
            "field so the rule lands on the right output slot"
        )

    def test_override_lists_concrete_capabilities(self, briefer_text: str):
        """The clause must enumerate at least a few concrete
        capability names so the model has anchors to recognize.
        Without examples the rule is too abstract for reliable
        application."""
        idx = briefer_text.find("Capability-match override")
        window = briefer_text[idx : idx + 1500]
        capabilities = ["OCR", "search", "transcribe"]
        present = [cap for cap in capabilities if cap in window]
        assert len(present) >= 2, (
            f"the override clause must enumerate concrete capability "
            f"examples (OCR, search, transcribe, navigate, ...). "
            f"Found {present}, expected ≥2."
        )
