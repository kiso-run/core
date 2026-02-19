"""Tests for kiso.cli_connector — connector management CLI commands."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiso.cli import build_parser
from kiso.cli_connector import (
    _connector_env_var_name,
    _validate_connector_manifest,
    discover_connectors,
    run_connector_command,
)
from kiso.config import User


# ── Helpers ──────────────────────────────────────────────────


def _admin_cfg():
    cfg = MagicMock()
    cfg.users = {"alice": User(role="admin")}
    return cfg


@pytest.fixture()
def mock_admin():
    """Patch load_config and getpass so _require_admin passes."""
    with (
        patch("kiso.cli_skill.load_config", return_value=_admin_cfg()),
        patch("kiso.cli_skill.getpass.getuser", return_value="alice"),
    ):
        yield


# ── Subparser parsing ────────────────────────────────────────


def test_parse_connector_list():
    parser = build_parser()
    args = parser.parse_args(["connector", "list"])
    assert args.command == "connector"
    assert args.connector_command == "list"


def test_parse_connector_search_no_query():
    parser = build_parser()
    args = parser.parse_args(["connector", "search"])
    assert args.connector_command == "search"
    assert args.query == ""


def test_parse_connector_search_with_query():
    parser = build_parser()
    args = parser.parse_args(["connector", "search", "discord"])
    assert args.connector_command == "search"
    assert args.query == "discord"


def test_parse_connector_install():
    parser = build_parser()
    args = parser.parse_args(["connector", "install", "discord"])
    assert args.connector_command == "install"
    assert args.target == "discord"
    assert args.name is None
    assert args.no_deps is False
    assert args.show_deps is False


def test_parse_connector_install_url_with_name():
    parser = build_parser()
    args = parser.parse_args([
        "connector", "install", "git@github.com:user/repo.git", "--name", "foo",
    ])
    assert args.target == "git@github.com:user/repo.git"
    assert args.name == "foo"


def test_parse_connector_install_no_deps():
    parser = build_parser()
    args = parser.parse_args(["connector", "install", "discord", "--no-deps"])
    assert args.no_deps is True


def test_parse_connector_install_show_deps():
    parser = build_parser()
    args = parser.parse_args(["connector", "install", "discord", "--show-deps"])
    assert args.show_deps is True


def test_parse_connector_update():
    parser = build_parser()
    args = parser.parse_args(["connector", "update", "discord"])
    assert args.connector_command == "update"
    assert args.target == "discord"


def test_parse_connector_update_all():
    parser = build_parser()
    args = parser.parse_args(["connector", "update", "all"])
    assert args.target == "all"


def test_parse_connector_remove():
    parser = build_parser()
    args = parser.parse_args(["connector", "remove", "discord"])
    assert args.connector_command == "remove"
    assert args.name == "discord"


def test_parse_connector_run():
    parser = build_parser()
    args = parser.parse_args(["connector", "run", "discord"])
    assert args.connector_command == "run"
    assert args.name == "discord"


def test_parse_connector_stop():
    parser = build_parser()
    args = parser.parse_args(["connector", "stop", "discord"])
    assert args.connector_command == "stop"
    assert args.name == "discord"


def test_parse_connector_status():
    parser = build_parser()
    args = parser.parse_args(["connector", "status", "discord"])
    assert args.connector_command == "status"
    assert args.name == "discord"


def test_parse_connector_no_subcommand():
    parser = build_parser()
    args = parser.parse_args(["connector"])
    assert args.command == "connector"
    assert args.connector_command is None


# ── run_connector_command dispatcher ─────────────────────────


def test_run_connector_command_no_subcommand(capsys):
    args = argparse.Namespace(connector_command=None)
    with pytest.raises(SystemExit, match="1"):
        run_connector_command(args)
    out = capsys.readouterr().out
    assert "usage:" in out


# ── _validate_connector_manifest ─────────────────────────────


def test_validate_connector_manifest_valid(tmp_path):
    (tmp_path / "run.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'discord'\n")
    manifest = {
        "kiso": {
            "type": "connector",
            "name": "discord",
            "connector": {"platform": "discord"},
        }
    }
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert errors == []


def test_validate_connector_manifest_missing_connector_section(tmp_path):
    (tmp_path / "run.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest = {"kiso": {"type": "connector", "name": "x"}}
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert any("kiso.connector" in e for e in errors)


def test_validate_connector_manifest_wrong_type(tmp_path):
    (tmp_path / "run.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest = {
        "kiso": {
            "type": "skill",
            "name": "x",
            "connector": {"platform": "x"},
        }
    }
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert any("'connector'" in e for e in errors)


def test_validate_connector_manifest_missing_run_py(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest = {
        "kiso": {
            "type": "connector",
            "name": "x",
            "connector": {"platform": "x"},
        }
    }
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert any("run.py" in e for e in errors)


# ── _connector_env_var_name ──────────────────────────────────


def test_connector_env_var_name():
    assert _connector_env_var_name("discord", "bot_token") == "KISO_CONNECTOR_DISCORD_BOT_TOKEN"
    assert _connector_env_var_name("my-bot", "api-key") == "KISO_CONNECTOR_MY_BOT_API_KEY"


# ── discover_connectors ─────────────────────────────────────


def test_discover_connectors_empty(tmp_path):
    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    assert discover_connectors(connectors_dir) == []


def test_discover_connectors_finds_connectors(tmp_path):
    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    d = connectors_dir / "discord"
    d.mkdir()
    (d / "kiso.toml").write_text(
        '[kiso]\ntype = "connector"\nname = "discord"\nversion = "0.1.0"\n'
        'description = "Discord bridge"\n\n'
        "[kiso.connector]\n"
        'platform = "discord"\n'
    )
    (d / "run.py").write_text("pass\n")
    (d / "pyproject.toml").write_text("[project]\nname = 'discord'\n")

    result = discover_connectors(connectors_dir)
    assert len(result) == 1
    assert result[0]["name"] == "discord"
    assert result[0]["version"] == "0.1.0"
    assert result[0]["platform"] == "discord"


def test_discover_connectors_skips_installing(tmp_path):
    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    d = connectors_dir / "discord"
    d.mkdir()
    (d / "kiso.toml").write_text(
        '[kiso]\ntype = "connector"\nname = "discord"\n'
        "[kiso.connector]\n"
        'platform = "discord"\n'
    )
    (d / "run.py").write_text("pass\n")
    (d / "pyproject.toml").write_text("[project]\nname = 'discord'\n")
    (d / ".installing").touch()

    result = discover_connectors(connectors_dir)
    assert result == []


# ── _connector_list ──────────────────────────────────────────


def test_connector_list_empty(capsys):
    from kiso.cli_connector import _connector_list

    with patch("kiso.cli_connector.discover_connectors", return_value=[]):
        _connector_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "No connectors installed." in out


def test_connector_list_shows_connectors(capsys):
    from kiso.cli_connector import _connector_list

    connectors = [
        {"name": "discord", "version": "0.1.0", "description": "Discord bridge"},
        {"name": "telegram", "version": "0.2.0", "description": "Telegram bridge"},
    ]
    with patch("kiso.cli_connector.discover_connectors", return_value=connectors):
        _connector_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "discord" in out
    assert "0.1.0" in out
    assert "Discord bridge" in out
    assert "telegram" in out


# ── _connector_search ────────────────────────────────────────


def test_connector_search_no_query(capsys):
    from kiso.cli_connector import _connector_search

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "items": [
            {"name": "connector-discord", "description": "Discord bridge"},
            {"name": "connector-telegram", "description": "Telegram bridge"},
        ],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        _connector_search(argparse.Namespace(query=""))

    out = capsys.readouterr().out
    assert "discord" in out
    assert "telegram" in out
    # Verify connector- prefix was stripped
    assert "connector-discord" not in out

    call_args = mock_get.call_args
    assert "org:kiso-run" in call_args[1]["params"]["q"]
    assert "topic:kiso-connector" in call_args[1]["params"]["q"]


def test_connector_search_with_query(capsys):
    from kiso.cli_connector import _connector_search

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "items": [{"name": "connector-discord", "description": "Discord bridge"}],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        _connector_search(argparse.Namespace(query="discord"))

    call_args = mock_get.call_args
    assert "discord" in call_args[1]["params"]["q"]


def test_connector_search_network_error(capsys):
    import httpx

    with (
        patch("httpx.get", side_effect=httpx.ConnectError("fail")),
        pytest.raises(SystemExit, match="1"),
    ):
        from kiso.cli_connector import _connector_search

        _connector_search(argparse.Namespace(query=""))
    out = capsys.readouterr().out
    assert "GitHub search failed" in out


def test_connector_search_no_results(capsys):
    from kiso.cli_connector import _connector_search

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": []}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_resp):
        _connector_search(argparse.Namespace(query="nonexistent"))
    out = capsys.readouterr().out
    assert "No connectors found." in out


# ── _connector_install ───────────────────────────────────────


def _fake_clone_with_connector_manifest(name="discord", desc="Discord bridge"):
    """Return a fake_clone function that writes a valid connector repo."""
    def fake_clone(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kiso.toml").write_text(
            f'[kiso]\ntype = "connector"\nname = "{name}"\n'
            f'description = "{desc}"\n'
            f"[kiso.connector]\n"
            f'platform = "{name}"\n'
        )
        (dest / "run.py").write_text("pass\n")
        (dest / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n")
        return subprocess.CompletedProcess(cmd, 0)
    return fake_clone


def _ok_run(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 0)


def test_connector_install_official(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    clone_fn = _fake_clone_with_connector_manifest()

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=run_dispatch),
    ):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "installed successfully" in out


def test_connector_install_unofficial_with_confirm(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    clone_fn = _fake_clone_with_connector_manifest("myconn", "Custom connector")

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=run_dispatch),
        patch("builtins.input", return_value="y"),
    ):
        args = argparse.Namespace(
            target="https://github.com/someone/myconn.git",
            name="myconn",
            no_deps=False,
            show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "unofficial" in out.lower()
    assert "installed successfully" in out


def test_connector_install_config_example_copy(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    def clone_with_config_example(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kiso.toml").write_text(
            '[kiso]\ntype = "connector"\nname = "discord"\n'
            "[kiso.connector]\n"
            'platform = "discord"\n'
        )
        (dest / "run.py").write_text("pass\n")
        (dest / "pyproject.toml").write_text("[project]\nname = 'discord'\n")
        (dest / "config.example.toml").write_text('kiso_api = "http://localhost:8333"\n')
        return subprocess.CompletedProcess(cmd, 0)

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_with_config_example(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=run_dispatch),
    ):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "config.example.toml" in out
    assert (connectors_dir / "discord" / "config.toml").exists()


def test_connector_install_already_installed(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "discord").mkdir()

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "already installed" in out


def test_connector_install_git_clone_failure_cleanup(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    def fake_clone_fail(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 1, stderr="fatal: repo not found")

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=fake_clone_fail),
        pytest.raises(SystemExit, match="1"),
    ):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "git clone failed" in out
    assert not (connectors_dir / "discord").exists()


def test_connector_install_show_deps(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_install

    deps_content = "#!/bin/bash\napt install something\n"

    def fake_clone(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "deps.sh").write_text(deps_content)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("subprocess.run", side_effect=fake_clone):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=True,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "apt install something" in out


# ── _connector_update ────────────────────────────────────────


def test_connector_update_single(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=_ok_run),
    ):
        _connector_update(argparse.Namespace(target="discord"))

    out = capsys.readouterr().out
    assert "updated" in out


def test_connector_update_all(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    for name in ["discord", "telegram"]:
        (connectors_dir / name).mkdir()

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=_ok_run),
    ):
        _connector_update(argparse.Namespace(target="all"))

    out = capsys.readouterr().out
    assert "discord" in out and "updated" in out
    assert "telegram" in out


def test_connector_update_nonexistent(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_update(argparse.Namespace(target="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_remove ────────────────────────────────────────


def test_connector_remove_existing(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_remove

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "discord").mkdir()

    with patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir):
        _connector_remove(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "removed" in out
    assert not (connectors_dir / "discord").exists()


def test_connector_remove_nonexistent(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_remove

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_remove(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_run ───────────────────────────────────────────


def test_connector_run_start_ok(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        _connector_run(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "started" in out
    assert "12345" in out
    assert (connector_dir / ".pid").read_text() == "12345"


def test_connector_run_already_running(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill"),  # os.kill(pid, 0) succeeds → process alive
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_run(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "already running" in out


def test_connector_run_nonexistent(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_run(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_stop ──────────────────────────────────────────


def test_connector_stop_ok(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")

    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        if sig == 0:
            # After SIGTERM, process is dead
            if any(s == signal.SIGTERM for _, s in kill_calls):
                raise ProcessLookupError()

    import signal

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=fake_kill),
        patch("time.sleep"),
    ):
        _connector_stop(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "stopped" in out
    assert not (connector_dir / ".pid").exists()


def test_connector_stop_not_running(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    # No .pid file

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_stop(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out


def test_connector_stop_nonexistent(tmp_path, mock_admin, capsys):
    from kiso.cli_connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_stop(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_status ────────────────────────────────────────


def test_connector_status_running(tmp_path, capsys):
    from kiso.cli_connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill"),  # os.kill(pid, 0) succeeds
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "running" in out
    assert "12345" in out


def test_connector_status_not_running(tmp_path, capsys):
    from kiso.cli_connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    # No .pid file

    with patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out


def test_connector_status_stale_pid(tmp_path, capsys):
    from kiso.cli_connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")

    with (
        patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=ProcessLookupError()),
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out
    assert "stale" in out
    assert not (connector_dir / ".pid").exists()
