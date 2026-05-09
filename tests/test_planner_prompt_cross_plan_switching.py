"""Content-lock for M1648 (replaces the prior M1649 lock).

`kiso/roles/planner.md` must contain a schema-level rule
distinguishing `args` (the input data pipe to an MCP method)
from descriptive fields (`detail`, `msg`). The "never embed
raw data" rule that applies to descriptive fields does NOT
apply to `args`; `args` MUST carry the actual values the
method needs to do its work, including data copied from prior
plan outputs.

This was identified by the 2026-05-08 prompt-first audit as
the root cause of the navigate→translate cross-plan flake:
the planner was reading "never embed raw data" as forbidding
inlining prior outputs into args, while no
`{{plan_output_N}}` substitution mechanism exists. The fix
clarifies the schema; this lock pins the clarification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"


@pytest.fixture(scope="module")
def planner_text() -> str:
    return (_ROLES_DIR / "planner.md").read_text()


class TestArgsIsDataPipeRule:
    """The schema-level rule must be present and structurally
    identifiable so future edits cannot silently drop it."""

    def test_anchor_phrase_present(self, planner_text: str):
        """Stable anchor phrase: '`args` carries data'. Removing
        it removes the rule's identity."""
        assert "`args` carries data" in planner_text, (
            "planner.md must contain an '`args` carries data' "
            "rule. Without it the planner conflates the "
            "'never embed raw data' guidance (intended for "
            "descriptive fields) with the args contract, and "
            "falls back to re-fetching prior data via the prior "
            "turn's MCP — see the M1648 prompt-first audit."
        )

    def test_distinguishes_args_from_descriptive_fields(
        self, planner_text: str,
    ):
        """The rule must explicitly mark `detail` and `msg` as
        the descriptive fields the 'no raw data' guidance
        targets, leaving args as the data pipe."""
        idx = planner_text.find("`args` carries data")
        assert idx != -1
        window = planner_text[idx : idx + 2500]
        assert "detail" in window and "msg" in window, (
            "the rule must name `detail` and `msg` as the fields "
            "the 'never embed raw data' guidance targets — "
            "without that contrast, the model still treats args "
            "as descriptive"
        )

    def test_clarifies_no_substitution_mechanism(
        self, planner_text: str,
    ):
        """The rule must spell out that there is NO
        `{{plan_output_N}}` substitution: the planner inlines
        prior data verbatim. This pre-empts the dead-end where
        the model tries to write a reference and waits for the
        worker to resolve it."""
        idx = planner_text.find("`args` carries data")
        window = planner_text[idx : idx + 2500]
        assert "plan_output" in window.lower() or "substitution" in window.lower(), (
            "the rule must explicitly address the absence of a "
            "`{{plan_output_N}}` / substitution mechanism, so "
            "the planner inlines verbatim instead of writing an "
            "unresolvable reference"
        )

    def test_compact_vs_large_data_distinction(
        self, planner_text: str,
    ):
        """The rule must distinguish compact text (inline into
        args) from large binary artifacts (pass a path / URI
        per the method's inputSchema). Without this, the
        planner could try to inline a megabyte of base64 into
        args and blow the prompt budget."""
        idx = planner_text.find("`args` carries data")
        window = planner_text[idx : idx + 2500]
        # Either word — "path" or "URI" or "inputSchema" — anchors
        # the large-data-by-reference part of the distinction.
        has_reference_path = (
            "path" in window.lower()
            or "uri" in window.lower()
            or "inputschema" in window.lower()
        )
        assert has_reference_path, (
            "the rule must distinguish compact-data-inline from "
            "large-data-by-reference (path / URI / inputSchema), "
            "otherwise the planner risks inlining a binary blob"
        )

    def test_no_test_specific_example(self, planner_text: str):
        """Generalist guard: the rule's example must NOT name
        the navigate→translate pair. That pair is the test
        scenario; using it as the canonical example would be
        overfitting per the project memory rule."""
        idx = planner_text.find("`args` carries data")
        # 2500-char window, so close that the example must be inside
        window = planner_text[idx : idx + 2500].lower()
        assert "navigate" not in window or "translate" not in window, (
            "the schema rule's example must not be the "
            "navigate→translate pair (that's the test scenario, "
            "using it as the canonical example would be "
            "overfitting per `feedback_no_overfit_no_flake.md`)"
        )

    def test_old_cross_plan_switching_rule_removed(self, planner_text: str):
        """The M1649 'Cross-plan MCP switching' rule pointed at
        the non-existent `{{plan_output_N}}` substitution. The
        prompt-first audit removed it; this lock prevents it
        being silently re-introduced under that label."""
        assert "Cross-plan MCP switching" not in planner_text, (
            "the M1649 'Cross-plan MCP switching' rule referenced "
            "a non-existent substitution mechanism and has been "
            "removed (see M1648 audit). Re-introducing it would "
            "re-instate the prompt contradiction; if the future "
            "introduces a real substitution mechanism, the rule "
            "must be re-written accordingly, not pasted back."
        )
