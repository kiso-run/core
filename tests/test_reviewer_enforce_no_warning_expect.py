"""M1651 — code-level enforcement of the reviewer "no-warning expect" rule.

`reviewer.md:13` already encodes the rule: when `expect` contains
"no warning/error" / "without warning/error" / "cleanly" AND the
output has a warning/error line, the review MUST be `replan`. The
reviewer-LLM occasionally returns `ok` despite this — surfaced by
the live-tier `test_warning_with_explicit_no_warnings_expect_returns_replan`
flake. This module pins the deterministic post-validation override
that closes the gap.

Pattern matches M1647: prompt rule first, code-level safety net to
guarantee determinism on top.
"""

from __future__ import annotations

import pytest


from kiso.brain.reviewer import _enforce_no_warning_expect


class TestExpectDemandsNoWarning:
    """Override fires when expect literally demands no-warning/error
    AND output contains a warning/error log line AND review.status=='ok'."""

    def test_no_warnings_phrase_overrides_ok_to_replan(self):
        review = {"status": "ok", "reason": "looks fine", "summary": ""}
        result = _enforce_no_warning_expect(
            review,
            expect="Wrapper installed with no warnings or errors",
            output="warning: KISO_WRAPPER_AIDER_API_KEY not set\nWrapper installed.",
        )
        assert result["status"] == "replan"
        assert "[enforced]" in result["reason"]

    def test_cleanly_phrase_overrides(self):
        review = {"status": "ok", "reason": "ok", "summary": ""}
        result = _enforce_no_warning_expect(
            review,
            expect="Server started cleanly",
            output="WARNING: deprecated config\nServer up",
        )
        assert result["status"] == "replan"

    def test_without_warning_phrase_overrides(self):
        review = {"status": "ok", "reason": "ok", "summary": ""}
        result = _enforce_no_warning_expect(
            review,
            expect="Compile without warnings",
            output="src.c:42: warning: unused variable\nbuild ok",
        )
        assert result["status"] == "replan"


class TestNoOverride:
    """Override is a no-op when the pattern doesn't match."""

    def test_no_warning_phrase_in_expect_passes_through(self):
        """Generic expect (no 'no warning' phrasing) → no override."""
        review = {"status": "ok", "reason": "fine"}
        result = _enforce_no_warning_expect(
            review,
            expect="Wrapper installed successfully",
            output="warning: API_KEY not set\nWrapper installed.",
        )
        assert result["status"] == "ok"

    def test_no_warning_in_output_passes_through(self):
        """Expect demands no-warning but output has none → no override."""
        review = {"status": "ok", "reason": "fine"}
        result = _enforce_no_warning_expect(
            review,
            expect="Wrapper installed with no warnings or errors",
            output="Wrapper installed successfully.\n",
        )
        assert result["status"] == "ok"

    def test_already_replan_preserved(self):
        """Reviewer already said replan → no double-override."""
        review = {"status": "replan", "reason": "previous reason"}
        result = _enforce_no_warning_expect(
            review,
            expect="Wrapper installed cleanly",
            output="warning: x",
        )
        assert result["status"] == "replan"
        assert result["reason"] == "previous reason"

    def test_already_stuck_preserved(self):
        """Stuck > replan; never downgrade or override."""
        review = {"status": "stuck", "reason": "needs human"}
        result = _enforce_no_warning_expect(
            review,
            expect="cleanly",
            output="warning: x",
        )
        assert result["status"] == "stuck"

    def test_warning_inside_prose_does_not_match(self):
        """A regular sentence mentioning the word 'warning' (not a
        log-formatted line) must NOT trigger. Only `warning:` / `WARNING:`
        / similar log prefixes count."""
        review = {"status": "ok"}
        result = _enforce_no_warning_expect(
            review,
            expect="Build completed cleanly",
            output="The build completed without any warning given.",
        )
        assert result["status"] == "ok"

    def test_libcurl_style_substring_does_not_match(self):
        """Word-boundary discipline: 'warned' / 'warner' / etc. must
        not match 'warning'."""
        review = {"status": "ok"}
        result = _enforce_no_warning_expect(
            review,
            expect="cleanly",
            output="Mr. Warner approved the change.\nbuilt ok",
        )
        assert result["status"] == "ok"


class TestErrorPattern:
    """The rule covers 'error' too, not just 'warning'."""

    def test_no_errors_phrase_with_error_line(self):
        review = {"status": "ok"}
        result = _enforce_no_warning_expect(
            review,
            expect="Run completed with no errors",
            output="error: missing config\npartial run",
        )
        assert result["status"] == "replan"


class TestEnforcedReasonShape:
    """The override's reason string must be diagnostic so the planner's
    replan can use it; it must mark itself as enforced (not LLM-emitted)."""

    def test_reason_marks_enforced_and_quotes_offender(self):
        review = {"status": "ok", "reason": "ok by reviewer"}
        result = _enforce_no_warning_expect(
            review,
            expect="cleanly",
            output="warning: deprecated\nbuilt ok",
        )
        assert "[enforced]" in result["reason"]
        # reason should mention the offending warning excerpt
        assert "warning" in result["reason"].lower()
