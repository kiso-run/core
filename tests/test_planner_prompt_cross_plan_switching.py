"""Content-lock for M1649.

`kiso/roles/planner.md` must contain an explicit invariant for
cross-plan MCP switching: when the user's intent in this turn
maps to a DIFFERENT installed MCP than the prior turn's, the
planner MUST emit an `mcp` task targeting the NEW MCP directly,
not re-run the prior turn's MCP first to "fetch the input data
again".

This locks the rule against silent removal — it's the prompt-side
companion to the M1647 deterministic capability augmenter (which
ensures the right MCP is *visible*; this rule ensures the planner
*uses it* across plan boundaries).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"


@pytest.fixture(scope="module")
def planner_text() -> str:
    return (_ROLES_DIR / "planner.md").read_text()


class TestCrossPlanMCPSwitchingRule:
    """The Cross-plan MCP switching invariant must be present and
    structurally identifiable so future edits cannot silently drop it."""

    def test_anchor_phrase_present(self, planner_text: str):
        """Stable anchor phrase used to locate the rule. Removing the
        phrase removes the rule's identity."""
        assert "Cross-plan MCP switching" in planner_text, (
            "planner.md must contain a 'Cross-plan MCP switching' "
            "invariant. Without it the planner sometimes re-routes "
            "to the prior turn's MCP on a new-intent message, "
            "breaking the M1647 capability augmenter's downstream "
            "guarantee."
        )

    def test_must_emit_directive(self, planner_text: str):
        """Hard directive (MUST) targeting the new MCP. Soft verbs
        ('should', 'consider') let the LLM downgrade the rule."""
        idx = planner_text.find("Cross-plan MCP switching")
        assert idx != -1
        window = planner_text[idx : idx + 1500]
        assert "MUST" in window, (
            "the rule must use MUST (capitalized) — the recency-bias "
            "failure mode survives soft suggestions"
        )

    def test_forbids_prior_mcp_re_run(self, planner_text: str):
        """The rule must explicitly forbid 're-running the prior
        turn's MCP first to fetch the input data again' — this is
        the specific failure mode observed in 2026-05-08 traces."""
        idx = planner_text.find("Cross-plan MCP switching")
        window = planner_text[idx : idx + 1500]
        # Either of these phrasings constitutes the forbidden-pattern
        # description. The exact wording is open; the structural
        # element is "do NOT re-run prior MCP".
        forbids = (
            "do NOT re-run" in window
            or "do not re-run" in window.lower()
            or "never re-run" in window.lower()
        )
        assert forbids, (
            "the rule must explicitly forbid re-running the prior "
            "turn's MCP just to obtain its output again — that "
            "output is already in the session workspace"
        )

    def test_references_session_workspace_or_output_indices(
        self, planner_text: str,
    ):
        """The rule must point the planner at the existing mechanism
        for reusing prior outputs (session workspace / output_indices /
        plan_output) so the model has a concrete alternative to
        'just call the prior MCP again'."""
        idx = planner_text.find("Cross-plan MCP switching")
        window = planner_text[idx : idx + 1500]
        has_pointer = (
            "session workspace" in window.lower()
            or "output_indices" in window.lower()
            or "plan_output" in window.lower()
            or "session_files" in window.lower()
        )
        assert has_pointer, (
            "the rule must point the planner at session workspace / "
            "output_indices / plan_output as the way to access the "
            "prior turn's output without re-running its MCP"
        )
