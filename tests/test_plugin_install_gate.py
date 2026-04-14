"""Unit tests for the probe-gated ``deps.sh`` re-run decision.

Gate semantics (see `cli.plugin_ops._gate_deps_decision`):

    --no-deps        → never run deps (user explicit opt-out)
    no deps.sh       → nothing to run
    --force          → always run (user explicit opt-in)
    pull brought new commits → run (source changed, trust the script)
    no health_check declared → run (default-safe, legacy behaviour)
    health_check exit == 0   → skip (system is healthy)
    health_check exit != 0   → run (probe failed, need repair)

The health-check invocation itself is separated into
``_run_health_check`` so the decision function stays pure and
trivially testable.
"""

from __future__ import annotations

import pytest

from cli.plugin_ops import _gate_deps_decision


class TestGateDepsDecision:
    def test_no_deps_flag_wins(self):
        """--no-deps always suppresses, regardless of other inputs."""
        run, reason = _gate_deps_decision(
            deps_path_exists=True,
            no_deps=True,
            force=True,
            pull_changed=True,
            health_check_cmd="true",
            health_check_result=False,
        )
        assert run is False
        assert "no-deps" in reason.lower()

    def test_missing_deps_script_skips(self):
        run, reason = _gate_deps_decision(
            deps_path_exists=False,
            no_deps=False,
            force=True,
            pull_changed=True,
            health_check_cmd="true",
            health_check_result=True,
        )
        assert run is False
        assert "no deps.sh" in reason.lower()

    def test_force_runs_even_when_healthy(self):
        """--force bypasses every skip condition except --no-deps."""
        run, reason = _gate_deps_decision(
            deps_path_exists=True,
            no_deps=False,
            force=True,
            pull_changed=False,
            health_check_cmd="true",
            health_check_result=True,
        )
        assert run is True
        assert "forced" in reason.lower()

    def test_pull_changed_runs_even_when_healthy(self):
        """A git pull that advanced HEAD runs deps.sh even if the
        probe would otherwise be green. New commits may have added
        new system deps the current probe doesn't cover."""
        run, reason = _gate_deps_decision(
            deps_path_exists=True,
            no_deps=False,
            force=False,
            pull_changed=True,
            health_check_cmd="true",
            health_check_result=True,
        )
        assert run is True
        assert "updated" in reason.lower() or "changed" in reason.lower()

    def test_no_health_check_runs(self):
        """Wrappers without a declared probe keep the current
        behaviour byte-for-byte — zero regression."""
        run, reason = _gate_deps_decision(
            deps_path_exists=True,
            no_deps=False,
            force=False,
            pull_changed=False,
            health_check_cmd=None,
            health_check_result=None,
        )
        assert run is True
        assert "no health_check" in reason.lower()

    def test_healthy_probe_skips(self):
        """Declared probe + probe green + source unchanged → skip."""
        run, reason = _gate_deps_decision(
            deps_path_exists=True,
            no_deps=False,
            force=False,
            pull_changed=False,
            health_check_cmd="chromium --version",
            health_check_result=True,
        )
        assert run is False
        assert "healthy" in reason.lower()

    def test_failing_probe_runs(self):
        """Declared probe + probe red → run deps.sh to repair."""
        run, reason = _gate_deps_decision(
            deps_path_exists=True,
            no_deps=False,
            force=False,
            pull_changed=False,
            health_check_cmd="chromium --version",
            health_check_result=False,
        )
        assert run is True
        assert "probe failed" in reason.lower() or "unhealthy" in reason.lower()


# ---------------------------------------------------------------------------
# _run_health_check — the side-effecting half
# ---------------------------------------------------------------------------


class TestRunHealthCheck:
    def test_returns_true_on_zero_exit(self, tmp_path):
        from cli.plugin_ops import _run_health_check
        assert _run_health_check("true", cwd=tmp_path) is True

    def test_returns_false_on_nonzero_exit(self, tmp_path):
        from cli.plugin_ops import _run_health_check
        assert _run_health_check("false", cwd=tmp_path) is False

    def test_returns_false_on_missing_command(self, tmp_path):
        from cli.plugin_ops import _run_health_check
        assert (
            _run_health_check(
                "definitely-not-a-real-binary-xyzzy-42", cwd=tmp_path,
            )
            is False
        )

    def test_returns_false_on_empty_command(self, tmp_path):
        """An empty string health_check is treated as 'no probe
        declared' — the helper returns False (probe effectively
        absent) and the caller falls through to the default
        'no health_check declared → run' branch."""
        from cli.plugin_ops import _run_health_check
        assert _run_health_check("", cwd=tmp_path) is False
