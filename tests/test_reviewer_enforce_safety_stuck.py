"""M1652 — code-level enforcement of the reviewer "safety violation = stuck" rule.

`reviewer.md:28` already encodes: "Safety compliance: if output
shows violation of a Safety Rule (when present), status must be
`stuck` with reason citing the violated rule." The reviewer-LLM
emitted `replan` instead in
`TestFRulesPathDisclosure.test_safety_rule_blocks_paths`, causing
an infinite replan loop because `_SAFETY_REFUSAL_DIRECTIVE` only
fires on `stuck`.

This module pins the deterministic post-validation override that
extracts literal patterns from safety facts and forces `stuck`
when an output contains any of them — same archetype as M1651
(prompt rule first, code-level safety net for determinism).
"""

from __future__ import annotations

import pytest

from kiso.brain.reviewer import (
    _extract_safety_patterns,
    _enforce_safety_violation_stuck,
)


# ---------------------------------------------------------------------------
# _extract_safety_patterns
# ---------------------------------------------------------------------------


class TestExtractSafetyPatterns:
    """Extract literal patterns the rule names. Path-like tokens
    (slash-bounded) are the primary signal; quoted tokens are also
    extracted. Free-form prose without patterns yields nothing."""

    def test_extracts_path_patterns(self):
        rule = (
            "SAFETY: Never reveal absolute filesystem paths like /home/, "
            "/root/, /var/, /etc/ in responses."
        )
        out = _extract_safety_patterns([rule])
        assert "/home/" in out
        assert "/root/" in out
        assert "/var/" in out
        assert "/etc/" in out

    def test_extracts_path_patterns_no_trailing_slash(self):
        rule = "SAFETY: Don't expose /opt/secrets or /var/log/auth.log."
        out = _extract_safety_patterns([rule])
        assert any(p.startswith("/opt") for p in out)
        assert any(p.startswith("/var/log") for p in out)

    def test_extracts_quoted_tokens(self):
        rule = "SAFETY: Never echo the secret 'PASSWD' or the token \"NDA\"."
        out = _extract_safety_patterns([rule])
        assert "PASSWD" in out
        assert "NDA" in out

    def test_no_patterns_when_rule_is_abstract(self):
        rule = "SAFETY: Be polite and never insult the user."
        out = _extract_safety_patterns([rule])
        # No paths, no quoted tokens → nothing to enforce on.
        assert out == []

    def test_multi_rule_aggregates(self):
        rules = [
            "SAFETY: Don't show /etc/passwd.",
            "SAFETY: Don't echo the literal 'API_KEY'.",
        ]
        out = _extract_safety_patterns(rules)
        assert any("/etc/passwd" in p for p in out)
        assert "API_KEY" in out


# ---------------------------------------------------------------------------
# _enforce_safety_violation_stuck
# ---------------------------------------------------------------------------


class TestEnforceSafetyViolationStuck:
    """Override fires when output contains a literal safety pattern
    AND review.status is anything other than already-stuck."""

    _PATH_RULE = (
        "SAFETY: Never reveal absolute filesystem paths like /home/, "
        "/root/, /var/, /etc/ in responses."
    )

    def test_override_replan_to_stuck_on_path_violation(self):
        review = {"status": "replan", "reason": "command failed"}
        output = "ls: /root/.config: No such file or directory\n"
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=[self._PATH_RULE]
        )
        assert result["status"] == "stuck"
        assert "[enforced]" in result["reason"]
        assert "/root/" in result["reason"]

    def test_override_ok_to_stuck_on_path_violation(self):
        """Even when the reviewer said `ok`, a leaked path forces stuck."""
        review = {"status": "ok"}
        output = "Configs are in /etc/kiso/config.toml\n"
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=[self._PATH_RULE]
        )
        assert result["status"] == "stuck"

    def test_already_stuck_preserved(self):
        review = {"status": "stuck", "reason": "human action needed"}
        output = "/root/secret leaked"
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=[self._PATH_RULE]
        )
        assert result["status"] == "stuck"
        assert result["reason"] == "human action needed"

    def test_no_override_when_output_does_not_contain_pattern(self):
        review = {"status": "ok"}
        output = "Operation complete.\n"
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=[self._PATH_RULE]
        )
        assert result["status"] == "ok"

    def test_no_override_when_no_safety_rules(self):
        review = {"status": "ok"}
        output = "ls: /root/.config: ..."
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=None
        )
        assert result["status"] == "ok"

    def test_no_override_when_rule_has_no_extractable_patterns(self):
        review = {"status": "ok"}
        output = "/root/.config exists"
        # Abstract rule has no enforceable patterns → no override.
        result = _enforce_safety_violation_stuck(
            review,
            output=output,
            safety_rules=["SAFETY: Be polite at all times."],
        )
        assert result["status"] == "ok"

    def test_reason_marks_enforced_and_cites_rule(self):
        review = {"status": "replan", "reason": "x"}
        output = "matched at /var/log/secrets.log"
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=[self._PATH_RULE]
        )
        # Reason must mark the override origin AND cite the rule
        assert "[enforced]" in result["reason"]
        assert "safety" in result["reason"].lower()

    def test_multi_rule_picks_matching_one(self):
        review = {"status": "ok"}
        output = "secret API_KEY=abc123"
        rules = [
            "SAFETY: Don't echo /etc/secrets.",
            "SAFETY: Never reveal 'API_KEY' value.",
        ]
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=rules
        )
        assert result["status"] == "stuck"
        assert "API_KEY" in result["reason"]

    def test_retry_hint_cleared(self):
        """A safety-violation override clears retry_hint — the planner
        must change strategy (refuse), not re-attempt."""
        review = {"status": "replan", "reason": "x", "retry_hint": "use sudo"}
        output = "matched /home/secret"
        result = _enforce_safety_violation_stuck(
            review, output=output, safety_rules=[self._PATH_RULE]
        )
        assert result["retry_hint"] is None
