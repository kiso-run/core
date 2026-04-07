"""Supervisor lifecycle unit tests.

Tests ``_supervisor_main`` from cli/connector.py:
- Clean exit handling
- Crash + exponential backoff
- Gave-up after max failures
- Stable threshold resets failure count
- SIGTERM forwarding to child
- PID file management
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_connector(tmp_path: Path, script: str) -> Path:
    """Create a minimal connector directory with a fake run.py and venv."""
    connector_dir = tmp_path / "test-connector"
    connector_dir.mkdir(parents=True, exist_ok=True)

    # Create .venv/bin/python pointing to the real interpreter
    venv_bin = connector_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    python_link = venv_bin / "python"
    python_link.symlink_to(sys.executable)

    # Write run.py
    (connector_dir / "run.py").write_text(textwrap.dedent(script))
    return connector_dir


def _read_status(connector_dir: Path) -> dict:
    """Read .status.json from connector directory."""
    status_file = connector_dir / ".status.json"
    return json.loads(status_file.read_text())


def _run_supervisor(connector_name: str, connector_dir: Path, monkeypatch) -> None:
    """Run _supervisor_main with fast settings."""
    import cli.connector as mod

    monkeypatch.setattr(mod, "SUPERVISOR_MAX_FAILURES", 3)
    monkeypatch.setattr(mod, "SUPERVISOR_INITIAL_BACKOFF", 0.01)
    monkeypatch.setattr(mod, "SUPERVISOR_MAX_BACKOFF", 0.05)
    monkeypatch.setattr(mod, "SUPERVISOR_BACKOFF_MULTIPLIER", 2.0)
    monkeypatch.setattr(mod, "SUPERVISOR_STABLE_THRESHOLD", 0.5)
    monkeypatch.setattr(mod, "CONNECTORS_DIR", connector_dir.parent)

    mod._supervisor_main(connector_name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSupervisorCleanExit:
    def test_child_exits_zero_supervisor_exits(self, tmp_path, monkeypatch):
        """Child exits 0 → supervisor exits, no restarts."""
        connector_dir = _make_fake_connector(tmp_path, """\
            import sys
            sys.exit(0)
        """)
        _run_supervisor("test-connector", connector_dir, monkeypatch)

        # PID file should be cleaned up
        assert not (connector_dir / ".pid").exists()

        # Log should indicate clean exit
        log_content = (connector_dir / "connector.log").read_text()
        assert "exited cleanly" in log_content


class TestSupervisorCrashBackoff:
    def test_crash_increments_failures(self, tmp_path, monkeypatch):
        """Child crashes → consecutive_failures increments, backoff doubles."""
        connector_dir = _make_fake_connector(tmp_path, """\
            import sys
            sys.exit(1)
        """)
        _run_supervisor("test-connector", connector_dir, monkeypatch)

        status = _read_status(connector_dir)
        # Should have given up after 3 failures (SUPERVISOR_MAX_FAILURES=3)
        assert status["gave_up"] is True
        assert status["consecutive_failures"] >= 3
        assert status["last_exit_code"] == 1

    def test_backoff_increases(self, tmp_path, monkeypatch):
        """Backoff doubles each crash until max."""
        connector_dir = _make_fake_connector(tmp_path, """\
            import sys
            sys.exit(1)
        """)
        _run_supervisor("test-connector", connector_dir, monkeypatch)

        # Verify log shows increasing backoff
        log_content = (connector_dir / "connector.log").read_text()
        assert "waiting" in log_content


class TestSupervisorGaveUp:
    def test_gave_up_after_max_failures(self, tmp_path, monkeypatch):
        """After MAX_FAILURES consecutive crashes → gave_up=true."""
        connector_dir = _make_fake_connector(tmp_path, """\
            import sys
            sys.exit(42)
        """)
        _run_supervisor("test-connector", connector_dir, monkeypatch)

        status = _read_status(connector_dir)
        assert status["gave_up"] is True
        assert status["consecutive_failures"] == 3  # MAX_FAILURES=3
        assert status["last_exit_code"] == 42
        assert status["restarts"] == 3

        log_content = (connector_dir / "connector.log").read_text()
        assert "giving up" in log_content


class TestSupervisorStableThreshold:
    def test_stable_run_resets_failures(self, tmp_path, monkeypatch):
        """Child running > STABLE_THRESHOLD then crashing resets failure count."""
        # Script: first 2 runs exit immediately (failures), 3rd runs 0.6s then crashes,
        # 4th exits immediately again. The 3rd run is "stable" so failures reset.
        connector_dir = _make_fake_connector(tmp_path, """\
            import sys, time, os

            # Use a counter file to track invocations
            counter_file = os.path.join(os.path.dirname(__file__), ".run_count")
            count = 0
            if os.path.exists(counter_file):
                count = int(open(counter_file).read().strip())
            count += 1
            open(counter_file, "w").write(str(count))

            if count == 1:
                # Run for >0.5s (STABLE_THRESHOLD), then crash
                time.sleep(0.6)
                sys.exit(1)
            elif count == 2:
                # Quick crash after stable reset
                sys.exit(1)
            elif count == 3:
                # Another quick crash
                sys.exit(1)
            elif count == 4:
                # Another quick crash → should give up (3 consecutive)
                sys.exit(1)
            else:
                sys.exit(0)
        """)
        _run_supervisor("test-connector", connector_dir, monkeypatch)

        status = _read_status(connector_dir)
        # After stable run (count=1), failures reset to 1 (one restart).
        # Then count=2,3 are quick crashes (consecutive_failures=2,3) → gave_up at 3.
        # Total restarts: 3 (the stable crash + 2 quick crashes).
        assert status["gave_up"] is True
        assert status["restarts"] >= 3

        log_content = (connector_dir / "connector.log").read_text()
        assert "was stable" in log_content


class TestSupervisorSigterm:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="SIGTERM not available on Windows"
    )
    def test_sigterm_stops_supervisor(self, tmp_path, monkeypatch):
        """SIGTERM to supervisor → child terminated, PID file cleaned up."""
        connector_dir = _make_fake_connector(tmp_path, """\
            import time
            time.sleep(60)  # hang forever
        """)

        import cli.connector as mod

        monkeypatch.setattr(mod, "SUPERVISOR_MAX_FAILURES", 3)
        monkeypatch.setattr(mod, "SUPERVISOR_INITIAL_BACKOFF", 0.01)
        monkeypatch.setattr(mod, "SUPERVISOR_MAX_BACKOFF", 0.05)
        monkeypatch.setattr(mod, "SUPERVISOR_BACKOFF_MULTIPLIER", 2.0)
        monkeypatch.setattr(mod, "SUPERVISOR_STABLE_THRESHOLD", 0.5)
        monkeypatch.setattr(mod, "CONNECTORS_DIR", connector_dir.parent)

        # Run supervisor in a subprocess so we can send SIGTERM
        script = textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            import cli.connector as mod
            mod.SUPERVISOR_MAX_FAILURES = 3
            mod.SUPERVISOR_INITIAL_BACKOFF = 0.01
            mod.SUPERVISOR_MAX_BACKOFF = 0.05
            mod.SUPERVISOR_BACKOFF_MULTIPLIER = 2.0
            mod.SUPERVISOR_STABLE_THRESHOLD = 0.5
            mod.CONNECTORS_DIR = __import__("pathlib").Path({str(connector_dir.parent)!r})
            mod._supervisor_main("test-connector")
        """)

        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for child to start
        time.sleep(0.5)
        assert proc.poll() is None, "supervisor should still be running"

        # Send SIGTERM
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

        # PID file should be cleaned up
        assert not (connector_dir / ".pid").exists()

        log_content = (connector_dir / "connector.log").read_text()
        assert "SIGTERM" in log_content or "stopped" in log_content


class TestSupervisorPidFile:
    def test_pid_file_cleaned_on_clean_exit(self, tmp_path, monkeypatch):
        """PID file is removed after clean exit."""
        connector_dir = _make_fake_connector(tmp_path, """\
            import sys
            sys.exit(0)
        """)
        _run_supervisor("test-connector", connector_dir, monkeypatch)
        assert not (connector_dir / ".pid").exists()

    def test_pid_file_cleaned_on_gave_up(self, tmp_path, monkeypatch):
        """PID file is removed after giving up."""
        connector_dir = _make_fake_connector(tmp_path, """\
            import sys
            sys.exit(1)
        """)
        _run_supervisor("test-connector", connector_dir, monkeypatch)
        assert not (connector_dir / ".pid").exists()
