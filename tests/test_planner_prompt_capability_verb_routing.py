"""Content-lock for the capability-verb-governs-routing planner rule.

When the user's CURRENT turn names a different capability verb
than the prior turn's primary MCP, the planner MUST route to the
NEW MCP — re-invoking the prior turn's MCP just because the
conversation context shows it last is recency bias, not routing.

This pairs with the augmenter's extra_context extension which
ensures the capability verb survives replan paraphrases at the
context-injection layer; the planner-prompt rule closes the gap
at the model-decision layer.

The lock pins the rule against silent removal — same posture
as `tests/test_planner_prompt_cross_plan_switching.py` (M1648)
and `tests/test_planner_prompt_discovery_consume.py` (M1658).
"""

from __future__ import annotations

from pathlib import Path

import pytest


_ROLES_DIR = Path(__file__).resolve().parents[1] / "kiso" / "roles"


@pytest.fixture
def planner_text() -> str:
    return (_ROLES_DIR / "planner.md").read_text()


class TestCapabilityVerbRoutingRule:
    """The capability-verb-governs-routing invariant must be present
    and structurally identifiable."""

    def test_anchor_phrase_present(self, planner_text: str):
        """Stable anchor phrase — removing it removes the rule's
        identity."""
        assert "Capability verb governs routing" in planner_text, (
            "planner.md must contain the 'Capability verb governs "
            "routing' rule. Without it the planner falls into "
            "recency bias on cross-turn MCP handoffs (e.g. "
            "navigate-then-translate re-uses browser-mcp instead "
            "of routing to translate-mcp)."
        )

    def test_rule_explicitly_forbids_recency_bias(self, planner_text: str):
        """The rule must explicitly call out the wrong shape so the
        planner sees the negative example."""
        assert "recency bias" in planner_text.lower(), (
            "rule must name 'recency bias' so the planner can "
            "self-recognize the failure mode."
        )

    def test_rule_references_data_pipe(self, planner_text: str):
        """The rule must point to args-as-data-pipe so the planner
        knows HOW to consume the prior turn's output without
        re-invoking the prior MCP."""
        assert "data-pipe rule" in planner_text or "args" in planner_text, (
            "rule must reference args / data-pipe so the planner "
            "consumes prior data via args, not by re-running the "
            "prior MCP."
        )

    def test_rule_lists_multilingual_verbs(self, planner_text: str):
        """The rule should illustrate with at least one non-English
        verb so the planner recognizes the multilingual case (the
        cross-plan failure is reproducible with 'traduci')."""
        assert "traduci" in planner_text, (
            "rule must include at least one non-English verb "
            "example — the augmenter normalizes IT/ES/FR/DE verbs "
            "and the planner must know the multilingual case is "
            "in scope."
        )
