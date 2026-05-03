"""M1579c — planner prompt ask-first + anti-overfitting locks.

The planner prompt today contains two latent problems for the
broker model:

1. **Hardcoded names** — line 91 "(e.g. perplexity / sonar)" — bake
   specific search-engine MCPs into the prompt. The model sees them
   as canonical and falls back to them when the catalog is empty.

2. **No explicit ask-first policy** — when a capability is missing
   the prompt says "propose installing one via needs_install + msg",
   but doesn't tell the planner to use the M1579a `awaits_input`
   field, doesn't forbid guessing URLs, and doesn't forbid pivoting
   to exec.

These static locks pin the post-revision shape:
- `awaits_input` literal present.
- ask-first phrasing visible.
- `perplexity` / `sonar` removed.
- `FORBIDDEN` block present, listing the 3 cited behaviors.
"""

from __future__ import annotations

from pathlib import Path

import pytest


PLANNER_MD = (
    Path(__file__).resolve().parent.parent / "kiso" / "roles" / "planner.md"
)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return PLANNER_MD.read_text()


class TestAskFirstPolicy:
    def test_awaits_input_referenced(self, prompt_text):
        assert "awaits_input" in prompt_text, (
            "planner.md must reference the M1579a `awaits_input` "
            "schema field"
        )

    def test_ask_first_phrasing_present(self, prompt_text):
        """Some variant of "ask the user for a URL or say search" must
        appear so the planner has a concrete script to follow when a
        capability is missing.

        M1612: the canonical "Do you have a specific URL..." wording
        was retired together with the duplicated "Capability missing —
        ask-first flow" section (Decision Tree branch 2 covers the
        same flow). The current wording — "Paste a URL to install
        ... or say `search`" — is semantically equivalent.
        """
        lower = prompt_text.lower()
        candidates = (
            "do you have a specific url",
            "do you have a url",
            "do you have a specific link",
            "paste a url to install",
            "paste a url",
        )
        assert any(c in lower for c in candidates), (
            "planner.md must include an explicit ask-first phrasing "
            "that asks the user for a URL or to search"
        )


class TestAntiOverfitting:
    @pytest.mark.parametrize("forbidden", ["perplexity", "sonar", "tavily"])
    def test_no_hardcoded_search_mcp_names(self, prompt_text, forbidden):
        assert forbidden.lower() not in prompt_text.lower(), (
            f"planner.md must not hardcode {forbidden!r}; use generic "
            f"phrasing like 'any installed search MCP' "
            f"(decision 6, anti-overfitting)"
        )


class TestForbiddenBlock:
    def test_forbidden_header_present(self, prompt_text):
        assert "FORBIDDEN" in prompt_text, (
            "planner.md must include an explicit FORBIDDEN block "
            "listing the three cited broker-model anti-patterns"
        )

    def test_forbidden_block_lists_three_behaviors(self, prompt_text):
        """The block must mention all three failure modes the M1579c
        retrospective identified."""
        lower = prompt_text.lower()
        assert "guessed the url" in lower or "guess the url" in lower, (
            "FORBIDDEN block must call out URL-guessing"
        )
        assert "pivot" in lower, (
            "FORBIDDEN block must call out exec-pivoting on missing MCP"
        )
        assert "search the web" in lower or "high-level" in lower, (
            "FORBIDDEN block must call out high-level intents that "
            "should not become exec"
        )


class TestInvestigateBranch:
    """M1619 — investigate mode (read-only diagnostic plans) is
    triggered by the planner's Decision Tree itself, not by an
    external `investigate=True` flag set by the classifier.

    These locks pin the post-M1619 prompt shape so the M1620
    classifier-deletion can land safely. They specifically check the
    `planning_rules` module (where the Decision Tree lives — always
    loaded), NOT the opt-in `investigate` module (which will be
    retired in M1620 once nobody sets the flag).
    """

    @pytest.fixture(scope="class")
    def planning_rules_text(self):
        from kiso.brain import _load_modular_prompt
        # Load only core + planning_rules so the test sees what an
        # actual planner call sees on a "default" diagnostic-intent
        # message, where the briefer never opts into the legacy
        # `investigate` module.
        return _load_modular_prompt("planner", ["planning_rules"]).lower()

    def test_decision_tree_has_diagnostic_branch(self, planning_rules_text):
        """The Decision Tree must contain a branch that fires when the
        user wants to inspect state without changing it. The branch
        explicitly mentions read-only / no-state-change semantics so
        the LLM has concrete guidance.
        """
        text = planning_rules_text
        has_diagnostic_intent = (
            "diagnose" in text
            or "diagnostic" in text
            or "inspect" in text
            or "investigate" in text
        )
        has_read_only = (
            "read-only" in text
            or "read only" in text
            or "no state change" in text
            or "do not change" in text
            or "do not modify" in text
        )
        assert has_diagnostic_intent and has_read_only, (
            "planning_rules (Decision Tree) must contain a branch / rule "
            "that pairs diagnostic intent (diagnose/inspect/investigate) "
            "with a read-only / no-state-change constraint"
        )

    def test_investigate_branch_lists_forbidden_mutations(self, planning_rules_text):
        """The diagnostic branch must explicitly forbid mutating
        commands (rm / mv / install / git commit / file edits) so the
        LLM has a concrete enumeration to obey, not just an abstract
        "read-only" label.
        """
        text = planning_rules_text
        has_rm_or_delete = "rm" in text or "delete" in text
        has_install_or_pkg = "install" in text or "package" in text
        has_commit_or_write = (
            "commit" in text or "code edit" in text
            or "edit code" in text or "write file" in text
            or "modify" in text
        )
        assert has_rm_or_delete and has_install_or_pkg and has_commit_or_write, (
            "diagnostic branch must enumerate forbidden mutations "
            "(rm/delete, install/package, commit/code-edit/modify) so "
            "the read-only constraint is concrete"
        )
