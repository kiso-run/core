"""Tests for the supervisor-config connector CLI.

Kiso no longer installs connector binaries. Connectors are declared
in ``config.toml`` under ``[connectors.<name>]`` with ``command`` /
``args`` / ``env`` / ``cwd`` / ``token`` / ``webhook`` fields.

Covers:

- ``kiso connector list`` against config-declared entries
- ``kiso connector start`` / ``stop`` / ``status`` / ``logs``
- ``kiso connector add`` (parity with ``kiso mcp add``)
- ``kiso connector migrate`` (legacy install hint)
- Supervisor lifecycle: clean exit, restart on crash, max-failures
  give-up, stable-run reset, SIGTERM, PID cleanup on exit
- ``discover_connectors()`` reading from config
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _cfg(**connectors) -> MagicMock:
    """Fake Config object with a .connectors dict."""
    c = MagicMock()
    c.connectors = connectors
    c.users = {}
    return c


def _connector_config(
    *,
    name: str = "discord",
    command: str = "uvx",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    enabled: bool = True,
):
    from kiso.connector_config import ConnectorConfig

    return ConnectorConfig(
        name=name,
        command=command,
        args=args or [],
        env=env or {},
        cwd=cwd,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_run_connector_command_no_subcommand(capsys):
    """Calling `kiso connector` with no subcommand surfaces the usage line."""
    from cli.connector import run_connector_command

    with pytest.raises(SystemExit):
        run_connector_command(_args(connector_command=None))
    err = capsys.readouterr().err
    assert "list" in err and "start" in err and "stop" in err


def test_run_connector_command_unknown(capsys):
    from cli.connector import run_connector_command

    with pytest.raises(SystemExit):
        run_connector_command(_args(connector_command="frobnicate"))
    err = capsys.readouterr().err
    assert "usage" in err


# ---------------------------------------------------------------------------
# discover_connectors — config-driven
# ---------------------------------------------------------------------------


class TestDiscoverConnectors:
    def test_empty_config(self):
        from kiso.connectors import discover_connectors

        assert discover_connectors(_cfg()) == []

    def test_single_connector(self):
        from kiso.connectors import discover_connectors

        cfg = _cfg(discord=_connector_config(args=["kiso-discord-connector"]))
        out = discover_connectors(cfg)
        assert out == [
            {
                "name": "discord",
                "description": "uvx kiso-discord-connector",
                "command": "uvx",
                "args": ["kiso-discord-connector"],
                "enabled": True,
            }
        ]

    def test_multiple_connectors_sorted_by_name(self):
        from kiso.connectors import discover_connectors

        cfg = _cfg(
            slack=_connector_config(name="slack", command="python"),
            discord=_connector_config(name="discord", command="uvx"),
        )
        names = [c["name"] for c in discover_connectors(cfg)]
        assert names == ["discord", "slack"]

    def test_disabled_connector_still_reported(self):
        from kiso.connectors import discover_connectors

        cfg = _cfg(discord=_connector_config(enabled=False))
        out = discover_connectors(cfg)
        assert out[0]["enabled"] is False


# ---------------------------------------------------------------------------
# kiso connector list
# ---------------------------------------------------------------------------


class TestConnectorList:
    def test_empty(self, capsys):
        from cli.connector import _connector_list

        with patch("cli.connector.discover_connectors", return_value=[]):
            _connector_list(_args())
        out = capsys.readouterr().out
        assert "no connectors configured" in out

    def test_lists_configured(self, capsys, tmp_path):
        from cli.connector import _connector_list

        rows = [
            {
                "name": "discord",
                "description": "uvx kiso-discord-connector",
                "command": "uvx",
                "args": ["kiso-discord-connector"],
                "enabled": True,
            }
        ]
        with (
            patch("cli.connector.discover_connectors", return_value=rows),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
        ):
            _connector_list(_args())
        out = capsys.readouterr().out
        assert "discord" in out
        assert "stopped" in out
        assert "uvx kiso-discord-connector" in out


# ---------------------------------------------------------------------------
# kiso connector start
# ---------------------------------------------------------------------------


class TestConnectorStart:
    def test_spawns_supervisor_and_writes_pid(self, tmp_path, monkeypatch):
        from cli.connector import _connector_start

        connector = _connector_config()
        state_dir = tmp_path / "discord"
        state_dir.mkdir()

        fake_proc = MagicMock()
        fake_proc.pid = 12345

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=connector),
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.subprocess.Popen", return_value=fake_proc) as popen,
        ):
            _connector_start(_args(name="discord"))

        assert (state_dir / ".pid").read_text() == "12345"
        popen.assert_called_once()
        # Spawn command: python -c 'from cli.connector import _supervisor_main; _supervisor_main("discord")'
        cmd = popen.call_args.args[0]
        assert "_supervisor_main" in cmd[2]
        assert "'discord'" in cmd[2]

    def test_refuses_when_already_running(self, tmp_path, capsys):
        from cli.connector import _connector_start

        connector = _connector_config()
        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        pid_file = state_dir / ".pid"
        pid_file.write_text("99999")

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=connector),
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.os.kill"),  # succeeds → pid alive
        ):
            with pytest.raises(SystemExit):
                _connector_start(_args(name="discord"))
        err = capsys.readouterr().out
        assert "already running" in err

    def test_refuses_disabled_connector(self, capsys):
        from cli.connector import _connector_start

        connector = _connector_config(enabled=False)

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=connector),
        ):
            with pytest.raises(SystemExit):
                _connector_start(_args(name="discord"))
        assert "disabled" in capsys.readouterr().err

    def test_clears_stale_pid(self, tmp_path):
        from cli.connector import _connector_start

        connector = _connector_config()
        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        pid_file = state_dir / ".pid"
        pid_file.write_text("99999")

        fake_proc = MagicMock()
        fake_proc.pid = 12345

        def _stale_kill(pid, sig):
            if pid == 99999:
                raise ProcessLookupError()

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=connector),
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.subprocess.Popen", return_value=fake_proc),
            patch("cli.connector.os.kill", side_effect=_stale_kill),
        ):
            _connector_start(_args(name="discord"))
        assert pid_file.read_text() == "12345"


# ---------------------------------------------------------------------------
# kiso connector stop
# ---------------------------------------------------------------------------


class TestConnectorStop:
    def test_stop_ok(self, tmp_path):
        from cli.connector import _connector_stop

        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / ".pid").write_text("12345")

        # os.kill alive-check succeeds twice (SIGTERM + first poll) then
        # ProcessLookupError indicates the process is gone.
        call = {"n": 0}

        def _kill(pid, sig):
            call["n"] += 1
            if call["n"] <= 1:  # SIGTERM
                return
            raise ProcessLookupError()  # subsequent alive-checks → gone

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.os.kill", side_effect=_kill),
            patch("cli.connector.time.sleep"),
        ):
            _connector_stop(_args(name="discord"))
        assert not (state_dir / ".pid").exists()

    def test_stop_not_running(self, tmp_path, capsys):
        from cli.connector import _connector_stop

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
        ):
            with pytest.raises(SystemExit):
                _connector_stop(_args(name="discord"))
        assert "not running" in capsys.readouterr().out

    def test_stop_stale_pid(self, tmp_path, capsys):
        from cli.connector import _connector_stop

        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / ".pid").write_text("99999")

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.os.kill", side_effect=ProcessLookupError()),
        ):
            with pytest.raises(SystemExit):
                _connector_stop(_args(name="discord"))
        assert not (state_dir / ".pid").exists()

    def test_stop_sigkill_fallback(self, tmp_path):
        from cli.connector import _connector_stop

        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / ".pid").write_text("12345")

        kill_calls: list[int] = []

        def _kill(pid, sig):
            kill_calls.append(sig)
            # Always alive — so SIGKILL should fire after the 50-iteration wait.

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.os.kill", side_effect=_kill),
            patch("cli.connector.time.sleep"),
        ):
            _connector_stop(_args(name="discord"))
        assert signal.SIGTERM in kill_calls
        assert signal.SIGKILL in kill_calls


# ---------------------------------------------------------------------------
# kiso connector status
# ---------------------------------------------------------------------------


class TestConnectorStatus:
    def test_not_running(self, tmp_path, capsys):
        from cli.connector import _connector_status

        with (
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
        ):
            _connector_status(_args(name="discord"))
        assert "not running" in capsys.readouterr().out

    def test_running_plain(self, tmp_path, capsys):
        from cli.connector import _connector_status

        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / ".pid").write_text("12345")

        with (
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.os.kill"),  # alive
        ):
            _connector_status(_args(name="discord"))
        out = capsys.readouterr().out
        assert "running" in out
        assert "12345" in out

    def test_running_with_restarts(self, tmp_path, capsys):
        from cli.connector import _connector_status

        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / ".pid").write_text("12345")
        (state_dir / ".status.json").write_text(json.dumps({"restarts": 3}))

        with (
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.os.kill"),
        ):
            _connector_status(_args(name="discord"))
        out = capsys.readouterr().out
        assert "Restarts: 3" in out

    def test_gave_up(self, tmp_path, capsys):
        from cli.connector import _connector_status

        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / ".pid").write_text("99999")
        (state_dir / ".status.json").write_text(
            json.dumps({"gave_up": True, "restarts": 5, "last_exit_code": 127})
        )

        with (
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
            patch("cli.connector.os.kill", side_effect=ProcessLookupError()),
        ):
            _connector_status(_args(name="discord"))
        out = capsys.readouterr().out
        assert "gave up" in out
        assert "127" in out


# ---------------------------------------------------------------------------
# kiso connector logs
# ---------------------------------------------------------------------------


class TestConnectorLogs:
    def test_no_log_yet(self, tmp_path, capsys):
        from cli.connector import _connector_logs

        with (
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
        ):
            _connector_logs(_args(name="discord", n=50))
        assert "no log yet" in capsys.readouterr().out

    def test_tails_last_n_lines(self, tmp_path, capsys):
        from cli.connector import _connector_logs

        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / "connector.log").write_text("\n".join(str(i) for i in range(10)))

        with (
            patch("cli.connector._load_connector", return_value=_connector_config()),
            patch("cli.connector.CONNECTORS_DIR", tmp_path),
        ):
            _connector_logs(_args(name="discord", n=3))
        out = capsys.readouterr().out.strip().splitlines()
        assert out == ["7", "8", "9"]


# ---------------------------------------------------------------------------
# kiso connector add
# ---------------------------------------------------------------------------


class TestConnectorAdd:
    def test_writes_minimal_entry(self, tmp_path):
        from cli.connector import _connector_add

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('[tokens]\nadmin = "t"\n[providers.p]\nbase_url = "u"\n[users.u]\nrole = "admin"\n')

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector.CONFIG_PATH", cfg_path),
        ):
            _connector_add(
                _args(
                    name="discord",
                    command="uvx",
                    args=["kiso-discord-connector"],
                    cwd=None,
                    env=None,
                    token=None,
                    webhook=None,
                )
            )
        body = cfg_path.read_text()
        assert "[connectors.discord]" in body
        assert 'command = "uvx"' in body

    def test_writes_full_entry(self, tmp_path):
        from cli.connector import _connector_add

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('[tokens]\nadmin = "t"\n[providers.p]\nbase_url = "u"\n[users.u]\nrole = "admin"\n')

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector.CONFIG_PATH", cfg_path),
        ):
            _connector_add(
                _args(
                    name="slack",
                    command="python",
                    args=["-m", "slack_connector"],
                    cwd="/opt/slack",
                    env=["SLACK_TOKEN=xoxb-abc"],
                    token="kiso-api-token",
                    webhook="http://localhost:9001/x",
                )
            )
        body = cfg_path.read_text()
        assert "[connectors.slack]" in body
        assert "SLACK_TOKEN" in body
        assert "/opt/slack" in body

    def test_rejects_invalid_name(self, tmp_path, capsys):
        from cli.connector import _connector_add

        with patch("cli.connector.require_admin"):
            with pytest.raises(SystemExit):
                _connector_add(
                    _args(
                        name="Bad-Name!",
                        command="uvx",
                        args=None,
                        cwd=None,
                        env=None,
                        token=None,
                        webhook=None,
                    )
                )
        assert "invalid connector name" in capsys.readouterr().err

    def test_rejects_malformed_env(self, tmp_path, capsys):
        from cli.connector import _connector_add

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('[tokens]\nadmin = "t"\n[providers.p]\nbase_url = "u"\n[users.u]\nrole = "admin"\n')

        with (
            patch("cli.connector.require_admin"),
            patch("cli.connector.CONFIG_PATH", cfg_path),
        ):
            with pytest.raises(SystemExit):
                _connector_add(
                    _args(
                        name="discord",
                        command="uvx",
                        args=None,
                        cwd=None,
                        env=["MISSING_EQUALS"],
                        token=None,
                        webhook=None,
                    )
                )
        assert "KEY=VAL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# kiso connector migrate
# ---------------------------------------------------------------------------


class TestConnectorMigrate:
    def test_no_legacy_dir(self, tmp_path, capsys):
        from cli.connector import _connector_migrate

        with patch("cli.connector.CONNECTORS_DIR", tmp_path / "nope"):
            _connector_migrate(_args())
        assert "no legacy" in capsys.readouterr().out

    def test_empty_legacy_dir(self, tmp_path, capsys):
        from cli.connector import _connector_migrate

        with patch("cli.connector.CONNECTORS_DIR", tmp_path):
            _connector_migrate(_args())
        assert "no legacy" in capsys.readouterr().out

    def test_suggests_block_from_legacy_run_py(self, tmp_path, capsys):
        from cli.connector import _connector_migrate

        legacy = tmp_path / "discord"
        legacy.mkdir()
        (legacy / "kiso.toml").write_text("[kiso]\nname='discord'\n")
        (legacy / "run.py").write_text("pass")

        with patch("cli.connector.CONNECTORS_DIR", tmp_path):
            _connector_migrate(_args())
        out = capsys.readouterr().out
        assert "[connectors.discord]" in out
        assert "run.py" in out


# ---------------------------------------------------------------------------
# Supervisor internals: _write_status
# ---------------------------------------------------------------------------


class TestWriteStatus:
    def test_creates_file(self, tmp_path):
        from cli.connector import _write_status

        _write_status(tmp_path, 3, 1, 2.0, False, 1)
        status = json.loads((tmp_path / ".status.json").read_text())
        assert status["restarts"] == 3
        assert status["consecutive_failures"] == 1

    def test_atomic_replace(self, tmp_path):
        from cli.connector import _write_status

        _write_status(tmp_path, 1, 0, 0.0, False, None)
        _write_status(tmp_path, 2, 0, 0.0, True, 3)
        status = json.loads((tmp_path / ".status.json").read_text())
        assert status["restarts"] == 2
        assert status["gave_up"] is True
        # No stray .tmp
        assert not (tmp_path / ".tmp").exists()


# ---------------------------------------------------------------------------
# Supervisor loop: _supervisor_main
# ---------------------------------------------------------------------------


class _FakeClock:
    """Auto-incrementing clock for supervisor tests.

    ``monotonic()`` advances by ``step`` each call. ``sleep()`` jumps the
    clock past any deadline so interruptible sleep loops exit after one
    iteration.
    """

    def __init__(self, step: float = 0.1):
        self._time = 0.0
        self._step = step

    def monotonic(self) -> float:
        self._time += self._step
        return self._time

    def sleep(self, seconds: float) -> None:
        self._time += seconds + 100.0


class TestSupervisorMain:
    def _setup(self, tmp_path):
        state_dir = tmp_path / "discord"
        state_dir.mkdir()
        (state_dir / "connector.log").touch()
        return state_dir

    def test_clean_exit_no_restart(self, tmp_path):
        from cli.connector import _supervisor_main

        state_dir = self._setup(tmp_path)
        connector = _connector_config()

        mock_child = MagicMock()
        mock_child.returncode = 0
        mock_child.wait.return_value = 0

        clock = _FakeClock()

        with (
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.subprocess.Popen", return_value=mock_child) as popen,
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord", connector=connector)

        # Popen got command + args from config
        assert popen.call_args.args[0] == ["uvx"]
        assert not (state_dir / ".pid").exists()
        assert not (state_dir / ".status.json").exists()

    def test_passes_args_and_env_to_child(self, tmp_path):
        from cli.connector import _supervisor_main

        state_dir = self._setup(tmp_path)
        connector = _connector_config(
            command="python",
            args=["-m", "slack_connector"],
            env={"SLACK_TOKEN": "xoxb-fake"},
            cwd="/opt/slack",
        )

        mock_child = MagicMock()
        mock_child.returncode = 0
        mock_child.wait.return_value = 0

        clock = _FakeClock()

        with (
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.subprocess.Popen", return_value=mock_child) as popen,
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("slack", connector=connector)

        call = popen.call_args
        assert call.args[0] == ["python", "-m", "slack_connector"]
        assert call.kwargs["cwd"] == "/opt/slack"
        # env must include connector env on top of os.environ.
        assert call.kwargs["env"]["SLACK_TOKEN"] == "xoxb-fake"

    def test_crash_and_restart(self, tmp_path):
        from cli.connector import _supervisor_main

        state_dir = self._setup(tmp_path)
        connector = _connector_config()

        child1 = MagicMock(returncode=1)
        child1.wait.return_value = 1
        child2 = MagicMock(returncode=0)
        child2.wait.return_value = 0

        clock = _FakeClock()

        with (
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.subprocess.Popen", side_effect=[child1, child2]),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord", connector=connector)

        status = json.loads((state_dir / ".status.json").read_text())
        assert status["restarts"] == 1
        assert status["gave_up"] is False

    def test_max_failures_gives_up(self, tmp_path):
        from cli.connector import SUPERVISOR_MAX_FAILURES, _supervisor_main

        state_dir = self._setup(tmp_path)
        connector = _connector_config()

        children = [MagicMock(returncode=1) for _ in range(SUPERVISOR_MAX_FAILURES)]
        for c in children:
            c.wait.return_value = 1

        clock = _FakeClock()

        with (
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.subprocess.Popen", side_effect=children),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord", connector=connector)

        status = json.loads((state_dir / ".status.json").read_text())
        assert status["gave_up"] is True
        assert status["restarts"] == SUPERVISOR_MAX_FAILURES

    def test_stable_run_resets_failure_count(self, tmp_path):
        from cli.connector import SUPERVISOR_STABLE_THRESHOLD, _supervisor_main

        state_dir = self._setup(tmp_path)
        connector = _connector_config()

        clock = _FakeClock()

        # child1: quick crash
        child1 = MagicMock(returncode=1)
        child1.wait.return_value = 1
        # child2: stable crash — advance clock past STABLE_THRESHOLD during wait
        child2 = MagicMock(returncode=1)

        def _wait_stable():
            clock._time += SUPERVISOR_STABLE_THRESHOLD + 10
            return 1

        child2.wait.side_effect = _wait_stable
        # child3: clean exit
        child3 = MagicMock(returncode=0)
        child3.wait.return_value = 0

        with (
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.subprocess.Popen", side_effect=[child1, child2, child3]),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord", connector=connector)

        status = json.loads((state_dir / ".status.json").read_text())
        assert status["restarts"] == 2
        assert status["consecutive_failures"] == 1

    def test_sigterm_forwards_and_cleans_up(self, tmp_path):
        from cli.connector import _supervisor_main

        state_dir = self._setup(tmp_path)
        connector = _connector_config()

        mock_child = MagicMock()
        mock_child.returncode = -15
        mock_child.poll.return_value = None

        captured = {}

        def _capture(signum, handler):
            if signum == signal.SIGTERM:
                captured["h"] = handler

        def _wait_and_sigterm():
            if "h" in captured:
                captured["h"](signal.SIGTERM, None)
            return -15

        mock_child.wait.side_effect = _wait_and_sigterm
        clock = _FakeClock()

        with (
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.subprocess.Popen", return_value=mock_child),
            patch("cli.connector.signal.signal", side_effect=_capture),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord", connector=connector)

        mock_child.terminate.assert_called_once()
        assert not (state_dir / ".pid").exists()

    def test_pid_cleaned_on_exception(self, tmp_path):
        from cli.connector import _supervisor_main

        state_dir = self._setup(tmp_path)
        (state_dir / ".pid").write_text("12345")
        connector = _connector_config()

        clock = _FakeClock()

        with (
            patch("cli.connector._state_dir", return_value=state_dir),
            patch("cli.connector.subprocess.Popen", side_effect=OSError("boom")),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
            pytest.raises(OSError),
        ):
            _supervisor_main("discord", connector=connector)
        assert not (state_dir / ".pid").exists()

    def test_missing_connector_name_raises(self, tmp_path):
        from cli.connector import _supervisor_main

        fake_cfg = MagicMock()
        fake_cfg.connectors = {}
        with patch("kiso.config.load_config", return_value=fake_cfg):
            with pytest.raises(SystemExit, match="not declared"):
                _supervisor_main("ghost")
