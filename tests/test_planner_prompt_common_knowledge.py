"""Content-lock for M1646.

`kiso/roles/planner.md` must contain an explicit Decision Tree
branch covering common-knowledge Q&A — questions whose answer is
stable conceptual knowledge the model already has from training
(definitions, fundamental concepts, established programming/CS
basics). Without this branch the planner is forced to either ask
the user to install a search MCP (branch 2) or fall through to
action plans, breaking pure-Q&A flows like TestF19.

This file pins the new branch text against accidental removal.
The pattern follows the M1619 content-lock convention: assert the
prompt contains the structural rules; do not assert wording
verbatim where it doesn't carry semantic weight.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"


@pytest.fixture(scope="module")
def planner_text() -> str:
    return (_ROLES_DIR / "planner.md").read_text()


class TestCommonKnowledgeBranch:
    """The Decision Tree must explicitly cover common, stable-knowledge
    Q&A so it can answer without hallucinating dynamic facts and
    without being forced into a search-MCP install ask."""

    def test_branch_5b_present(self, planner_text: str):
        """Branch label '5b.' appears in the Decision Tree, signaling
        a sub-branch sandwiched between the existing 5 (kb facts)
        and 6 (turn-closer)."""
        assert "5b." in planner_text, (
            "planner.md must contain a Decision Tree branch labelled "
            "`5b.` covering common-knowledge Q&A. Without this branch, "
            "TestF19 (pure English Q&A about recursion) is stochastic "
            "because the planner has no clean route for "
            "answer-from-training."
        )

    def test_branch_5b_uses_kb_answer(self, planner_text: str):
        """Branch 5b's structural action is a msg-only plan with
        kb_answer: true. The flag is reused (not a new schema field)
        so existing validation continues to fire."""
        # Locate the 5b block, then check kb_answer appears within it.
        text = planner_text
        idx = text.find("5b.")
        assert idx != -1, "branch 5b not found"
        # 5b's text runs until the next numbered branch (line starting "6.").
        # Use a generous window — 1500 chars covers the rule + scope clause.
        window = text[idx : idx + 1500]
        assert "kb_answer" in window, (
            "branch 5b must direct the planner to set "
            "`kb_answer: true` for common-knowledge answers"
        )

    def test_branch_5b_has_scope_limit(self, planner_text: str):
        """Branch 5b must explicitly restrict scope to STABLE knowledge
        and exclude dynamic / specific-entity queries (which still
        belong to the search-MCP capability rule). Without this clause,
        the branch turns into an unbounded 'guess from training'
        license that hallucinates on dynamic facts."""
        text = planner_text
        idx = text.find("5b.")
        window = text[idx : idx + 2000]
        # Either word — "stable" or "dynamic" — must appear, framing the
        # scope split. The exact wording is open; the semantic split is
        # the structural lock.
        has_stable = "stable" in window.lower()
        has_dynamic_exclusion = (
            "dynamic" in window.lower()
            or "current" in window.lower()
            or "today" in window.lower()
            or "latest version" in window.lower()
        )
        assert has_stable and has_dynamic_exclusion, (
            "branch 5b must (a) describe its scope as stable / common "
            "knowledge AND (b) exclude dynamic / current / "
            "latest-version queries. Without the scope split the rule "
            "becomes a hallucination license."
        )

    def test_line_56_defers_to_5b(self, planner_text: str):
        """The existing line-56 rule ('Info questions without file
        creation and no relevant fact: search MCP if installed,
        otherwise ask for install URL') must reference 5b so the
        common-knowledge case is checked FIRST and only specific /
        dynamic queries fall through to search-MCP."""
        # Find the line-56 anchor phrase and check 5b is mentioned in
        # its vicinity.
        anchor_idx = planner_text.find("Info questions without file creation")
        assert anchor_idx != -1, (
            "anchor phrase 'Info questions without file creation' must "
            "still be present (it pins the rule's identity)"
        )
        window = planner_text[anchor_idx : anchor_idx + 600]
        assert "5b" in window, (
            "the line-56 info-questions rule must reference branch 5b "
            "so common-knowledge questions take that branch first, "
            "before falling through to search-MCP / install-ask."
        )
