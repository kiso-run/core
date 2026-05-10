"""Content-lock for the discovery+consume planner rule.

`kiso/roles/planner.md` must contain an explicit rule for the
discovery+consume pattern: when an exec step produces data that
the next step (an MCP call) can consume directly via `args`,
the planner MUST emit the two tasks in the SAME plan, not
`[exec(find), replan]`.

Without this rule the planner falls back to the generic "lack
info → replan" guidance, emits exec-then-replan, and triggers
the circular replan detector when the replan reason text
overlaps with prior failures (observed in the OCR e2e test
where plan 4 emitted `[exec(find image), replan(call OCR with
the path)]` and got stuck after the find succeeded).

The lock pins the rule against silent removal — same posture
as `tests/test_planner_prompt_cross_plan_switching.py` for
M1648.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_ROLES_DIR = Path(__file__).resolve().parents[1] / "kiso" / "roles"


@pytest.fixture
def planner_text() -> str:
    return (_ROLES_DIR / "planner.md").read_text()


class TestDiscoveryConsumeRule:
    """The discovery+consume invariant must be present and
    structurally identifiable so future edits cannot silently drop it.
    """

    def test_anchor_phrase_present(self, planner_text: str):
        """Stable anchor phrase. Removing it removes the rule's
        identity."""
        assert "Discovery+consume pattern" in planner_text, (
            "planner.md must contain the 'Discovery+consume pattern' "
            "rule. Without it the planner emits "
            "`[exec(find), replan]` instead of `[exec(find), mcp(consume), msg]` "
            "for chained discovery → MCP-consume flows."
        )

    def test_rule_forbids_exec_then_replan_when_mcp_consume_available(
        self, planner_text: str,
    ):
        """The rule must explicitly call out the wrong shape so the
        planner sees the negative example."""
        # The forbidden shape pattern. Match flexibly — we want the
        # words "exec" and "replan" both negated near the rule.
        assert "never `[exec(find), replan]`" in planner_text, (
            "planner.md must explicitly forbid the "
            "`[exec(find), replan]` shape when an MCP consume call "
            "could resolve the next step directly."
        )

    def test_rule_references_args_data_pipe(self, planner_text: str):
        """The rule must point back to the args-is-data-pipe rule
        (M1648) so the planner knows args is where data flows."""
        assert "args-is-data-pipe rule" in planner_text, (
            "Discovery+consume rule must cross-reference the "
            "args-is-data-pipe rule — that's the mechanism by which "
            "the discovered path/value reaches the consuming MCP."
        )

    def test_rule_names_workspace_and_plan_outputs_as_data_sources(
        self, planner_text: str,
    ):
        """The rule must name the two visible-data sources the
        planner can rely on without re-discovery: ## Session
        Workspace and ## Plan Outputs."""
        assert "Session Workspace" in planner_text
        assert "Plan Outputs" in planner_text
