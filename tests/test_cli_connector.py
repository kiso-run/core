"""Tests for kiso.cli_connector — connector management CLI commands."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli import build_parser
from cli.connector import (
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
    """Patch load_config and getpass so require_admin passes."""
    with (
        patch("cli.plugin_ops.load_config", return_value=_admin_cfg()),
        patch("cli.plugin_ops.getpass.getuser", return_value="alice"),
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
    from cli.connector import _connector_list

    with patch("cli.connector.discover_connectors", return_value=[]):
        _connector_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "No connectors installed." in out


def test_connector_list_shows_connectors(capsys):
    from cli.connector import _connector_list

    connectors = [
        {"name": "discord", "version": "0.1.0", "description": "Discord bridge"},
        {"name": "telegram", "version": "0.2.0", "description": "Telegram bridge"},
    ]
    with patch("cli.connector.discover_connectors", return_value=connectors):
        _connector_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "discord" in out
    assert "0.1.0" in out
    assert "Discord bridge" in out
    assert "telegram" in out


# ── _connector_search ────────────────────────────────────────


FAKE_REGISTRY = {
    "skills": [
        {"name": "search", "description": "Web search"},
    ],
    "connectors": [
        {"name": "discord", "description": "Discord bridge with message splitting"},
        {"name": "telegram", "description": "Telegram bridge"},
    ],
}


def test_connector_search_no_query(capsys):
    from cli.connector import _connector_search

    with patch("cli.connector.fetch_registry", return_value=FAKE_REGISTRY):
        _connector_search(argparse.Namespace(query=""))

    out = capsys.readouterr().out
    assert "discord" in out
    assert "telegram" in out


def test_connector_search_by_name(capsys):
    from cli.connector import _connector_search

    with patch("cli.connector.fetch_registry", return_value=FAKE_REGISTRY):
        _connector_search(argparse.Namespace(query="discord"))

    out = capsys.readouterr().out
    assert "discord" in out
    assert "telegram" not in out


def test_connector_search_by_description(capsys):
    from cli.connector import _connector_search

    with patch("cli.connector.fetch_registry", return_value=FAKE_REGISTRY):
        _connector_search(argparse.Namespace(query="splitting"))

    out = capsys.readouterr().out
    assert "discord" in out
    assert "telegram" not in out


def test_connector_search_network_error(capsys):
    with (
        patch("cli.connector.fetch_registry", side_effect=SystemExit(1)),
        pytest.raises(SystemExit, match="1"),
    ):
        from cli.connector import _connector_search

        _connector_search(argparse.Namespace(query=""))


def test_connector_search_no_results(capsys):
    from cli.connector import _connector_search

    with patch("cli.connector.fetch_registry", return_value=FAKE_REGISTRY):
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
    from cli.connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    clone_fn = _fake_clone_with_connector_manifest()

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=run_dispatch),
    ):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "installed successfully" in out


def test_connector_install_env_warning_includes_description(tmp_path, mock_admin, capsys):
    """M46: install output includes env var description so the agent can relay it to the user."""
    from cli.connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    def clone_with_env(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kiso.toml").write_text(
            '[kiso]\ntype = "connector"\nname = "discord"\n'
            "[kiso.connector]\n"
            'platform = "discord"\n'
            "[kiso.connector.env]\n"
            'bot_token = { required = true, description = "Get this from discord.com/developers" }\n'
            'webhook_secret = { required = false, description = "Any random string" }\n'
        )
        (dest / "run.py").write_text("pass\n")
        (dest / "pyproject.toml").write_text("[project]\nname = 'discord'\n")
        return subprocess.CompletedProcess(cmd, 0)

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_with_env(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=run_dispatch),
    ):
        args = argparse.Namespace(target="discord", name=None, no_deps=False, show_deps=False)
        _connector_install(args)

    out = capsys.readouterr().out
    assert "KISO_CONNECTOR_DISCORD_BOT_TOKEN not set (required)" in out
    assert "Get this from discord.com/developers" in out
    assert "KISO_CONNECTOR_DISCORD_WEBHOOK_SECRET not set (optional)" in out
    assert "Any random string" in out
    assert "installed successfully" in out
    # required/optional labels must be present so the planner can distinguish them
    assert "(required)" in out
    assert "(optional)" in out


def test_connector_install_unofficial_with_confirm(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    clone_fn = _fake_clone_with_connector_manifest("myconn", "Custom connector")

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
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
    from cli.connector import _connector_install

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
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
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
    from cli.connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "discord").mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "already installed" in out


def test_connector_install_git_clone_failure_cleanup(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    def fake_clone_fail(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 1, stderr="fatal: repo not found")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=fake_clone_fail),
        pytest.raises(SystemExit, match="1"),
    ):
        args = argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        )
        _connector_install(args)

    out = capsys.readouterr().out
    assert "not found" in out
    assert not (connectors_dir / "discord").exists()


def test_connector_install_show_deps(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_install

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
    from cli.connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=_ok_run),
    ):
        _connector_update(argparse.Namespace(target="discord"))

    out = capsys.readouterr().out
    assert "updated" in out


def test_connector_update_all(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    for name in ["discord", "telegram"]:
        (connectors_dir / name).mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=_ok_run),
    ):
        _connector_update(argparse.Namespace(target="all"))

    out = capsys.readouterr().out
    assert "discord" in out and "updated" in out
    assert "telegram" in out


def test_connector_update_nonexistent(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_update(argparse.Namespace(target="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_remove ────────────────────────────────────────


def test_connector_remove_existing(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_remove

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "discord").mkdir()

    with patch("cli.connector.CONNECTORS_DIR", connectors_dir):
        _connector_remove(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "removed" in out
    assert not (connectors_dir / "discord").exists()


def test_connector_remove_nonexistent(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_remove

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_remove(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_run ───────────────────────────────────────────


def test_connector_run_start_ok(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
    ):
        _connector_run(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "started" in out
    assert "12345" in out
    assert (connector_dir / ".pid").read_text() == "12345"
    # Verify supervisor is spawned (not run.py directly)
    popen_args = mock_popen.call_args[0][0]
    assert "_supervisor_main" in popen_args[2]


def test_connector_run_clears_old_status(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".status.json").write_text('{"gave_up": true}')

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        _connector_run(argparse.Namespace(name="discord"))

    assert not (connector_dir / ".status.json").exists()


def test_connector_run_already_running(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill"),  # os.kill(pid, 0) succeeds → process alive
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_run(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "already running" in out


def test_connector_run_nonexistent(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_run(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_stop ──────────────────────────────────────────


def test_connector_stop_ok(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_stop

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
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=fake_kill),
        patch("time.sleep"),
    ):
        _connector_stop(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "stopped" in out
    assert not (connector_dir / ".pid").exists()


def test_connector_stop_not_running(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    # No .pid file

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_stop(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out


def test_connector_stop_nonexistent(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_stop(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


# ── _connector_status ────────────────────────────────────────


def test_connector_status_running(tmp_path, capsys):
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill"),  # os.kill(pid, 0) succeeds
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "running" in out
    assert "12345" in out


def test_connector_status_running_with_restarts(tmp_path, capsys):
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")
    (connector_dir / ".status.json").write_text(
        '{"restarts": 3, "consecutive_failures": 1, "gave_up": false}'
    )

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill"),
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "running" in out
    assert "Restarts: 3" in out


def test_connector_status_not_running(tmp_path, capsys):
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    # No .pid file

    with patch("cli.connector.CONNECTORS_DIR", connectors_dir):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out


def test_connector_status_stale_pid(tmp_path, capsys):
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=ProcessLookupError()),
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out
    assert "stale" in out
    assert not (connector_dir / ".pid").exists()


def test_connector_status_gave_up(tmp_path, capsys):
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")
    (connector_dir / ".status.json").write_text(
        '{"restarts": 5, "consecutive_failures": 5, "gave_up": true, "last_exit_code": 1}'
    )

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=ProcessLookupError()),
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out
    assert "gave up" in out.lower()
    assert "5 restarts" in out


# ── _supervisor_main ─────────────────────────────────────────

import json
import signal


class _FakeClock:
    """Auto-incrementing clock for supervisor tests.

    ``monotonic()`` advances by ``step`` each call. ``sleep()`` jumps the clock
    past any deadline so interruptible sleep loops exit after one iteration.
    """

    def __init__(self, step: float = 0.1):
        self._time = 0.0
        self._step = step

    def monotonic(self) -> float:
        self._time += self._step
        return self._time

    def sleep(self, seconds: float) -> None:
        self._time += seconds + 100.0  # jump well past any deadline


class TestSupervisorMain:
    """Tests for the supervisor restart loop."""

    def _make_connector_dir(self, tmp_path):
        connectors_dir = tmp_path / "connectors"
        connectors_dir.mkdir()
        connector_dir = connectors_dir / "discord"
        connector_dir.mkdir()
        (connector_dir / "connector.log").touch()
        return connectors_dir, connector_dir

    def test_clean_exit_no_restart(self, tmp_path):
        """Child exits with code 0 — supervisor exits, no restarts."""
        from cli.connector import _supervisor_main

        connectors_dir, connector_dir = self._make_connector_dir(tmp_path)

        mock_child = MagicMock()
        mock_child.returncode = 0
        mock_child.wait.return_value = 0

        clock = _FakeClock()

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("subprocess.Popen", return_value=mock_child),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord")

        assert not (connector_dir / ".pid").exists()
        assert not (connector_dir / ".status.json").exists()

    def test_crash_and_restart(self, tmp_path):
        """Child crashes once, gets restarted, then exits clean."""
        from cli.connector import _supervisor_main

        connectors_dir, connector_dir = self._make_connector_dir(tmp_path)

        child1 = MagicMock()
        child1.returncode = 1
        child1.wait.return_value = 1

        child2 = MagicMock()
        child2.returncode = 0
        child2.wait.return_value = 0

        clock = _FakeClock()

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("subprocess.Popen", side_effect=[child1, child2]),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord")

        status = json.loads((connector_dir / ".status.json").read_text())
        assert status["restarts"] == 1
        assert status["gave_up"] is False

    def test_max_failures_gives_up(self, tmp_path):
        """After SUPERVISOR_MAX_FAILURES consecutive quick crashes, supervisor gives up."""
        from cli.connector import (
            SUPERVISOR_MAX_FAILURES,
            _supervisor_main,
        )

        connectors_dir, connector_dir = self._make_connector_dir(tmp_path)

        children = []
        for _ in range(SUPERVISOR_MAX_FAILURES):
            child = MagicMock()
            child.returncode = 1
            child.wait.return_value = 1
            children.append(child)

        clock = _FakeClock()  # step=0.1 → all crashes are "quick" (<60s elapsed)

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("subprocess.Popen", side_effect=children),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord")

        status = json.loads((connector_dir / ".status.json").read_text())
        assert status["gave_up"] is True
        assert status["restarts"] == SUPERVISOR_MAX_FAILURES
        assert status["consecutive_failures"] == SUPERVISOR_MAX_FAILURES

    def test_stable_run_resets_failure_count(self, tmp_path):
        """If child ran for >= STABLE_THRESHOLD before crashing, consecutive failures reset."""
        from cli.connector import SUPERVISOR_STABLE_THRESHOLD, _supervisor_main

        connectors_dir, connector_dir = self._make_connector_dir(tmp_path)

        clock = _FakeClock()

        # child1: quick crash
        child1 = MagicMock()
        child1.returncode = 1
        child1.wait.return_value = 1

        # child2: stable crash — advance clock past STABLE_THRESHOLD during wait
        child2 = MagicMock()
        child2.returncode = 1
        def _wait_stable():
            clock._time += SUPERVISOR_STABLE_THRESHOLD + 10
            return 1
        child2.wait.side_effect = _wait_stable

        # child3: clean exit
        child3 = MagicMock()
        child3.returncode = 0
        child3.wait.return_value = 0

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("subprocess.Popen", side_effect=[child1, child2, child3]),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord")

        status = json.loads((connector_dir / ".status.json").read_text())
        assert status["restarts"] == 2
        assert status["consecutive_failures"] == 1  # reset after stable run

    def test_sigterm_stops_supervisor(self, tmp_path):
        """SIGTERM forwarded to child, supervisor exits cleanly."""
        from cli.connector import _supervisor_main

        connectors_dir, connector_dir = self._make_connector_dir(tmp_path)

        mock_child = MagicMock()
        mock_child.returncode = -15
        mock_child.poll.return_value = None

        sigterm_handler = None

        def capture_signal(signum, handler):
            nonlocal sigterm_handler
            if signum == signal.SIGTERM:
                sigterm_handler = handler

        def wait_and_sigterm():
            if sigterm_handler:
                sigterm_handler(signal.SIGTERM, None)
            return -15

        mock_child.wait.side_effect = wait_and_sigterm

        clock = _FakeClock()

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("subprocess.Popen", return_value=mock_child),
            patch("cli.connector.signal.signal", side_effect=capture_signal),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord")

        mock_child.terminate.assert_called_once()
        assert not (connector_dir / ".pid").exists()

    def test_crash_with_exception_during_popen(self, tmp_path):
        """If Popen raises, supervisor still cleans up PID file."""
        from cli.connector import _supervisor_main

        connectors_dir, connector_dir = self._make_connector_dir(tmp_path)
        (connector_dir / ".pid").write_text("12345")

        clock = _FakeClock()

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("subprocess.Popen", side_effect=OSError("no such file")),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
            pytest.raises(OSError),
        ):
            _supervisor_main("discord")

        # PID file should still be cleaned up via finally block
        assert not (connector_dir / ".pid").exists()

    def test_pid_file_cleaned_on_exit(self, tmp_path):
        """PID file is always removed when supervisor exits."""
        from cli.connector import _supervisor_main

        connectors_dir, connector_dir = self._make_connector_dir(tmp_path)
        (connector_dir / ".pid").write_text("12345")

        mock_child = MagicMock()
        mock_child.returncode = 0
        mock_child.wait.return_value = 0

        clock = _FakeClock()

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("subprocess.Popen", return_value=mock_child),
            patch("cli.connector.time.monotonic", side_effect=clock.monotonic),
            patch("cli.connector.time.sleep", side_effect=clock.sleep),
        ):
            _supervisor_main("discord")

        assert not (connector_dir / ".pid").exists()


# ── Edge cases: manifest validation ─────────────────────


def test_validate_manifest_kiso_not_dict(tmp_path):
    """If [kiso] is not a dict, return error and bail."""
    manifest = {"kiso": "not-a-dict"}
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert any("missing [kiso]" in e for e in errors)


def test_validate_manifest_kiso_missing(tmp_path):
    """No [kiso] at all."""
    errors = _validate_connector_manifest({}, tmp_path)
    assert any("missing [kiso]" in e for e in errors)


def test_validate_manifest_name_wrong_type(tmp_path):
    """kiso.name is an int instead of a string."""
    (tmp_path / "run.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest = {
        "kiso": {
            "type": "connector",
            "name": 42,
            "connector": {"platform": "x"},
        }
    }
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert any("name" in e for e in errors)


def test_validate_manifest_name_empty(tmp_path):
    """kiso.name is an empty string."""
    (tmp_path / "run.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest = {
        "kiso": {
            "type": "connector",
            "name": "",
            "connector": {"platform": "x"},
        }
    }
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert any("name" in e for e in errors)


def test_validate_manifest_missing_pyproject(tmp_path):
    """Missing pyproject.toml."""
    (tmp_path / "run.py").write_text("pass\n")
    manifest = {
        "kiso": {
            "type": "connector",
            "name": "x",
            "connector": {"platform": "x"},
        }
    }
    errors = _validate_connector_manifest(manifest, tmp_path)
    assert any("pyproject" in e for e in errors)


# ── Edge cases: discover_connectors ─────────────────────


def test_discover_connectors_no_dir():
    """If connectors dir doesn't exist, return empty list."""
    from pathlib import Path

    result = discover_connectors(Path("/nonexistent_path_xyz_12345"))
    assert result == []


def test_discover_connectors_corrupted_toml(tmp_path, caplog):
    """M37: corrupted kiso.toml is skipped and a warning is logged."""
    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    d = connectors_dir / "broken"
    d.mkdir()
    (d / "kiso.toml").write_text("this is not valid TOML [[[")

    import logging
    with caplog.at_level(logging.WARNING, logger="kiso.connectors"):
        result = discover_connectors(connectors_dir)
    assert result == []
    assert any("broken" in r.message and "kiso.toml" in r.message for r in caplog.records), (
        f"Expected warning about broken connector, got: {[r.message for r in caplog.records]}"
    )


def test_discover_connectors_invalid_manifest_skipped(tmp_path, caplog):
    """M37: valid TOML but invalid manifest is skipped with a warning."""
    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    d = connectors_dir / "bad"
    d.mkdir()
    (d / "kiso.toml").write_text('[kiso]\nname = "bad"\ntype = "skill"\n')
    (d / "run.py").write_text("pass\n")
    (d / "pyproject.toml").write_text("[project]\nname = 'bad'\n")

    import logging
    with caplog.at_level(logging.WARNING, logger="kiso.connectors"):
        result = discover_connectors(connectors_dir)
    assert result == []
    assert any("bad" in r.message for r in caplog.records), (
        f"Expected warning about bad connector, got: {[r.message for r in caplog.records]}"
    )


def test_discover_connectors_skips_files(tmp_path):
    """Non-directory entries in connectors dir should be skipped."""
    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "README.md").write_text("not a connector")

    result = discover_connectors(connectors_dir)
    assert result == []


# ── Edge cases: _connector_stop ─────────────────────────


def test_connector_stop_corrupt_pid_file(tmp_path, mock_admin, capsys):
    """PID file contains non-numeric content."""
    from cli.connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("not-a-number")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_stop(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out


def test_connector_stop_stale_pid(tmp_path, mock_admin, capsys):
    """SIGTERM fails because process is already dead."""
    from cli.connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=ProcessLookupError()),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_stop(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out
    assert "stale" in out


def test_connector_stop_sigkill_fallback(tmp_path, mock_admin, capsys):
    """Process ignores SIGTERM — falls through to SIGKILL."""
    from cli.connector import _connector_stop

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")

    kill_count = [0]

    def fake_kill(pid, sig):
        kill_count[0] += 1
        if sig == signal.SIGKILL:
            return

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=fake_kill),
        patch("time.sleep"),
    ):
        _connector_stop(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "stopped" in out
    assert kill_count[0] >= 52  # SIGTERM + 50 polls + SIGKILL


# ── Edge cases: _connector_status ───────────────────────


def test_connector_status_nonexistent(tmp_path, capsys):
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_status(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


def test_connector_status_corrupt_pid_file(tmp_path, capsys):
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("garbage")

    with patch("cli.connector.CONNECTORS_DIR", connectors_dir):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "not running" in out
    assert not (connector_dir / ".pid").exists()


def test_connector_status_corrupted_status_json(tmp_path, capsys):
    """Corrupted .status.json gracefully ignored."""
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")
    (connector_dir / ".status.json").write_text("{invalid json")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill"),
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "running" in out


def test_connector_status_zero_restarts(tmp_path, capsys):
    """Zero restarts — no restart info shown."""
    from cli.connector import _connector_status

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")
    (connector_dir / ".status.json").write_text('{"restarts": 0, "gave_up": false}')

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill"),
    ):
        _connector_status(argparse.Namespace(name="discord"))

    out = capsys.readouterr().out
    assert "running" in out
    assert "Restarts" not in out


# ── Edge cases: _connector_remove ───────────────────────


def test_connector_remove_stops_running(tmp_path, mock_admin, capsys):
    """Remove sends SIGTERM to running connector."""
    from cli.connector import _connector_remove

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("12345")

    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=fake_kill),
    ):
        _connector_remove(argparse.Namespace(name="discord"))

    assert "removed" in capsys.readouterr().out
    assert (12345, signal.SIGTERM) in killed


def test_connector_remove_stale_pid(tmp_path, mock_admin, capsys):
    """Remove with stale PID — still removes dir."""
    from cli.connector import _connector_remove

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=ProcessLookupError()),
    ):
        _connector_remove(argparse.Namespace(name="discord"))

    assert "removed" in capsys.readouterr().out
    assert not connector_dir.exists()


# ── Edge cases: _connector_install ──────────────────────


def test_connector_install_show_deps_no_deps_file(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_install

    def fake_clone(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("subprocess.run", side_effect=fake_clone):
        _connector_install(argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=True,
        ))

    assert "No deps.sh" in capsys.readouterr().out


def test_connector_install_show_deps_clone_fails(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_install

    def fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stderr="fatal: not found")

    with (
        patch("subprocess.run", side_effect=fail),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_install(argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=True,
        ))

    assert "not found" in capsys.readouterr().out


def test_connector_install_missing_kiso_toml(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    def fake_clone(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=fake_clone),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_install(argparse.Namespace(
            target="discord", name=None, no_deps=False, show_deps=False,
        ))

    out = capsys.readouterr().out
    assert "kiso.toml not found" in out
    assert not (connectors_dir / "discord").exists()


def test_connector_install_unofficial_declined(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_install

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    clone_fn = _fake_clone_with_connector_manifest("myconn", "Custom")

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=run_dispatch),
        patch("builtins.input", return_value="n"),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_install(argparse.Namespace(
            target="https://github.com/someone/myconn.git",
            name="myconn", no_deps=False, show_deps=False,
        ))

    assert "cancelled" in capsys.readouterr().out.lower()
    assert not (connectors_dir / "myconn").exists()


# ── Edge cases: _connector_update ───────────────────────


def test_connector_update_all_no_dir(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_update

    with patch("cli.connector.CONNECTORS_DIR", tmp_path / "nonexistent"):
        _connector_update(argparse.Namespace(target="all"))

    assert "No connectors installed" in capsys.readouterr().out


def test_connector_update_all_empty_dir(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()

    with patch("cli.connector.CONNECTORS_DIR", connectors_dir):
        _connector_update(argparse.Namespace(target="all"))

    assert "No connectors installed" in capsys.readouterr().out


def test_connector_update_git_pull_failure(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_update

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    (connectors_dir / "discord").mkdir()

    def fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stderr="merge conflict")

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("subprocess.run", side_effect=fail),
        pytest.raises(SystemExit, match="1"),
    ):
        _connector_update(argparse.Namespace(target="discord"))

    assert "git pull failed" in capsys.readouterr().out


# ── Edge cases: _connector_run ──────────────────────────


def test_connector_run_stale_pid_cleaned(tmp_path, mock_admin, capsys):
    from cli.connector import _connector_run

    connectors_dir = tmp_path / "connectors"
    connectors_dir.mkdir()
    connector_dir = connectors_dir / "discord"
    connector_dir.mkdir()
    (connector_dir / ".pid").write_text("99999")

    mock_proc = MagicMock()
    mock_proc.pid = 54321

    with (
        patch("cli.connector.CONNECTORS_DIR", connectors_dir),
        patch("os.kill", side_effect=ProcessLookupError()),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        _connector_run(argparse.Namespace(name="discord"))

    assert "started" in capsys.readouterr().out
    assert (connector_dir / ".pid").read_text() == "54321"
