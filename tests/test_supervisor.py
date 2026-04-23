"""Supervisor lifecycle unit tests with real subprocesses.

Tests ``cli.connector._supervisor_main`` against live child processes
(Python scripts written to tmp_path) under the supervisor-config model:
the supervisor spawns ``ConnectorConfig.command + args`` with its own
``env`` / ``cwd``, rather than hardcoding ``.venv/bin/python run.py``.

Covered:
- Clean exit (code 0) → supervisor exits without restart
- Crash → consecutive_failures + backoff doubling
- Give-up after MAX_FAILURES
- Stable-run resets failure count
- SIGTERM forwarded to child, PID cleaned up
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path


import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_connector(
    tmp_path: Path, script: str, name: str = "test-connector"
) -> tuple["ConnectorConfig", Path]:
    """Build a ConnectorConfig pointing at a Python test script + state dir.

    Returns (connector, state_dir). The state dir is ``tmp_path/<name>/``
    where supervisor writes ``.pid``, ``.status.json``, ``connector.log``.
    The script lives at ``tmp_path/runner.py`` and is run by ``sys.executable``.
    """
    from kiso.connector_config import ConnectorConfig

    state_dir = tmp_path / name
    state_dir.mkdir(parents=True, exist_ok=True)
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(script))

    connector = ConnectorConfig(
        name=name,
        command=sys.executable,
        args=[str(runner)],
        cwd=str(tmp_path),
    )
    return connector, state_dir


def _read_status(state_dir: Path) -> dict:
    return json.loads((state_dir / ".status.json").read_text())


def _run_supervisor(
    connector,
    state_dir: Path,
    monkeypatch,
) -> None:
    """Run ``_supervisor_main`` with fast supervisor settings."""
    import cli.connector as mod

    monkeypatch.setattr(mod, "SUPERVISOR_MAX_FAILURES", 3)
    monkeypatch.setattr(mod, "SUPERVISOR_INITIAL_BACKOFF", 0.01)
    monkeypatch.setattr(mod, "SUPERVISOR_MAX_BACKOFF", 0.05)
    monkeypatch.setattr(mod, "SUPERVISOR_BACKOFF_MULTIPLIER", 2.0)
    monkeypatch.setattr(mod, "SUPERVISOR_STABLE_THRESHOLD", 0.5)
    monkeypatch.setattr(mod, "CONNECTORS_DIR", state_dir.parent)

    mod._supervisor_main(connector.name, connector=connector)


# ---------------------------------------------------------------------------
# Clean exit
# ---------------------------------------------------------------------------


class TestSupervisorCleanExit:
    def test_child_exits_zero_supervisor_exits(self, tmp_path, monkeypatch):
        """Child exits 0 → supervisor exits, no restarts."""
        connector, state_dir = _make_fake_connector(
            tmp_path,
            """\
            import sys
            sys.exit(0)
            """,
        )
        _run_supervisor(connector, state_dir, monkeypatch)

        assert not (state_dir / ".pid").exists()
        log_content = (state_dir / "connector.log").read_text()
        assert "exited cleanly" in log_content


# ---------------------------------------------------------------------------
# Crash + backoff
# ---------------------------------------------------------------------------


class TestSupervisorCrashBackoff:
    def test_crash_increments_failures(self, tmp_path, monkeypatch):
        connector, state_dir = _make_fake_connector(
            tmp_path,
            """\
            import sys
            sys.exit(1)
            """,
        )
        _run_supervisor(connector, state_dir, monkeypatch)

        status = _read_status(state_dir)
        assert status["gave_up"] is True
        assert status["consecutive_failures"] >= 3
        assert status["last_exit_code"] == 1

    def test_backoff_increases(self, tmp_path, monkeypatch):
        connector, state_dir = _make_fake_connector(
            tmp_path,
            """\
            import sys
            sys.exit(1)
            """,
        )
        _run_supervisor(connector, state_dir, monkeypatch)
        log_content = (state_dir / "connector.log").read_text()
        assert "waiting" in log_content


# ---------------------------------------------------------------------------
# Gave up
# ---------------------------------------------------------------------------


class TestSupervisorGaveUp:
    def test_gave_up_after_max_failures(self, tmp_path, monkeypatch):
        connector, state_dir = _make_fake_connector(
            tmp_path,
            """\
            import sys
            sys.exit(42)
            """,
        )
        _run_supervisor(connector, state_dir, monkeypatch)

        status = _read_status(state_dir)
        assert status["gave_up"] is True
        assert status["consecutive_failures"] == 3
        assert status["last_exit_code"] == 42
        assert status["restarts"] == 3
        log_content = (state_dir / "connector.log").read_text()
        assert "giving up" in log_content


# ---------------------------------------------------------------------------
# Stable threshold
# ---------------------------------------------------------------------------


class TestSupervisorStableThreshold:
    def test_stable_run_resets_failures(self, tmp_path, monkeypatch):
        """Child running > STABLE_THRESHOLD then crashing resets failure count."""
        connector, state_dir = _make_fake_connector(
            tmp_path,
            """\
            import sys, time, os

            counter_file = os.path.join(os.path.dirname(__file__), ".run_count")
            count = 0
            if os.path.exists(counter_file):
                count = int(open(counter_file).read().strip())
            count += 1
            open(counter_file, "w").write(str(count))

            if count == 1:
                time.sleep(0.6)
                sys.exit(1)
            else:
                sys.exit(1)
            """,
        )
        _run_supervisor(connector, state_dir, monkeypatch)

        status = _read_status(state_dir)
        assert status["gave_up"] is True
        assert status["restarts"] >= 3
        log_content = (state_dir / "connector.log").read_text()
        assert "was stable" in log_content


# ---------------------------------------------------------------------------
# SIGTERM
# ---------------------------------------------------------------------------


class TestSupervisorSigterm:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="SIGTERM not available on Windows"
    )
    def test_sigterm_stops_supervisor(self, tmp_path):
        """SIGTERM to supervisor → child terminated, PID file cleaned up.

        The supervisor must run in a subprocess so we can actually send
        SIGTERM to it; inline invocation would signal the test runner.
        """
        state_dir = tmp_path / "test-connector"
        state_dir.mkdir(parents=True, exist_ok=True)
        runner = tmp_path / "runner.py"
        runner.write_text("import time\ntime.sleep(60)\n")

        launcher = textwrap.dedent(
            f"""\
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            import cli.connector as mod
            from kiso.connector_config import ConnectorConfig

            mod.SUPERVISOR_MAX_FAILURES = 3
            mod.SUPERVISOR_INITIAL_BACKOFF = 0.01
            mod.SUPERVISOR_MAX_BACKOFF = 0.05
            mod.SUPERVISOR_BACKOFF_MULTIPLIER = 2.0
            mod.SUPERVISOR_STABLE_THRESHOLD = 0.5
            mod.CONNECTORS_DIR = __import__("pathlib").Path({str(state_dir.parent)!r})

            connector = ConnectorConfig(
                name="test-connector",
                command={sys.executable!r},
                args=[{str(runner)!r}],
            )
            mod._supervisor_main("test-connector", connector=connector)
            """
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", launcher],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)
        assert proc.poll() is None, "supervisor should still be running"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

        assert not (state_dir / ".pid").exists()
        log_content = (state_dir / "connector.log").read_text()
        assert "SIGTERM" in log_content or "stopped" in log_content


# ---------------------------------------------------------------------------
# PID cleanup
# ---------------------------------------------------------------------------


class TestSupervisorPidFile:
    def test_pid_file_cleaned_on_clean_exit(self, tmp_path, monkeypatch):
        connector, state_dir = _make_fake_connector(
            tmp_path,
            """\
            import sys
            sys.exit(0)
            """,
        )
        _run_supervisor(connector, state_dir, monkeypatch)
        assert not (state_dir / ".pid").exists()

    def test_pid_file_cleaned_on_gave_up(self, tmp_path, monkeypatch):
        connector, state_dir = _make_fake_connector(
            tmp_path,
            """\
            import sys
            sys.exit(1)
            """,
        )
        _run_supervisor(connector, state_dir, monkeypatch)
        assert not (state_dir / ".pid").exists()
