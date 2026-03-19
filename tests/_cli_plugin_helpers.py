"""Shared test infrastructure for CLI plugin tests (tool + connector)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli import build_parser
from kiso.config import User


# ── Shared parse tests (M601) ──

# Each tuple: (plugin_type, subcommand, extra_args, checks)
# checks: list of (attr_suffix, expected) — attr = f"{plugin_type}_command" for subcommand
_PARSE_CASES = [
    ("tool", "list", [], []),
    ("tool", "search", [], [("query", "")]),
    ("tool", "search", ["web"], [("query", "web")]),
    ("tool", "install", ["search"], [("target", "search"), ("no_deps", False), ("show_deps", False)]),
    ("tool", "install", ["search", "--no-deps"], [("no_deps", True)]),
    ("tool", "install", ["search", "--show-deps"], [("show_deps", True)]),
    ("tool", "update", ["search"], [("target", "search")]),
    ("tool", "update", ["all"], [("target", "all")]),
    ("tool", "remove", ["search"], [("name", "search")]),
    ("connector", "list", [], []),
    ("connector", "search", [], [("query", "")]),
    ("connector", "search", ["discord"], [("query", "discord")]),
    ("connector", "install", ["discord"], [("target", "discord"), ("no_deps", False)]),
    ("connector", "install", ["discord", "--no-deps"], [("no_deps", True)]),
    ("connector", "install", ["discord", "--show-deps"], [("show_deps", True)]),
    ("connector", "update", ["discord"], [("target", "discord")]),
    ("connector", "update", ["all"], [("target", "all")]),
    ("connector", "remove", ["discord"], [("name", "discord")]),
    ("connector", "run", ["discord"], [("name", "discord")]),
    ("connector", "stop", ["discord"], [("name", "discord")]),
    ("connector", "status", ["discord"], [("name", "discord")]),
]


@pytest.mark.parametrize(
    "plugin_type,subcommand,extra_args,checks", _PARSE_CASES,
    ids=[f"{c[0]}_{c[1]}{'_' + '_'.join(c[2]) if c[2] else ''}" for c in _PARSE_CASES],
)
def test_parse_plugin_subcommand(plugin_type, subcommand, extra_args, checks):
    """Shared argparse test for all plugin subcommands."""
    parser = build_parser()
    args = parser.parse_args([plugin_type, subcommand] + extra_args)
    assert args.command == plugin_type
    cmd_attr = f"{plugin_type}_command"
    assert getattr(args, cmd_attr) == subcommand
    for attr, expected in checks:
        assert getattr(args, attr) == expected, f"{attr}={getattr(args, attr)!r}, expected {expected!r}"


@pytest.mark.parametrize("plugin_type", ["tool", "connector"])
def test_parse_plugin_no_subcommand(plugin_type):
    """No subcommand → command attr is None."""
    parser = build_parser()
    args = parser.parse_args([plugin_type])
    assert getattr(args, f"{plugin_type}_command") is None


@pytest.mark.parametrize("plugin_type", ["tool", "connector"])
def test_parse_plugin_install_url_with_name(plugin_type):
    """--name flag works with URL target."""
    parser = build_parser()
    args = parser.parse_args([plugin_type, "install", "git@github.com:user/repo.git", "--name", "foo"])
    assert args.target == "git@github.com:user/repo.git"
    assert args.name == "foo"


def _admin_cfg():
    """Mock config with an admin user."""
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


def _ok_run(cmd, **kwargs):
    """Always-succeeds subprocess mock."""
    return subprocess.CompletedProcess(cmd, 0)


def fake_clone_plugin(plugin_type: str, name: str = "test", **extra_manifest):
    """Unified plugin clone factory for both tool and connector tests.

    Returns a callable suitable for ``subprocess.run`` side_effect.
    """
    def fake_clone(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        if plugin_type == "connector":
            desc = extra_manifest.get("description", f"{name} bridge")
            (dest / "kiso.toml").write_text(
                f'[kiso]\ntype = "connector"\nname = "{name}"\n'
                f'description = "{desc}"\n'
                f"[kiso.connector]\n"
                f'platform = "{name}"\n'
            )
        else:
            summary = extra_manifest.get("summary", f"{name} tool")
            usage = extra_manifest.get("usage_guide", "Use default guidance.")
            (dest / "kiso.toml").write_text(
                f'[kiso]\ntype = "tool"\nname = "{name}"\n'
                f"[kiso.tool]\n"
                f'summary = "{summary}"\n'
                f'usage_guide = "{usage}"\n'
            )
        (dest / "run.py").write_text("pass\n")
        (dest / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n")
        return subprocess.CompletedProcess(cmd, 0)
    return fake_clone
