"""Shared test infrastructure for CLI plugin tests (tool + connector)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiso.config import User


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
                f'[kiso]\ntype = "skill"\nname = "{name}"\n'
                f"[kiso.skill]\n"
                f'summary = "{summary}"\n'
                f'usage_guide = "{usage}"\n'
            )
        (dest / "run.py").write_text("pass\n")
        (dest / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n")
        return subprocess.CompletedProcess(cmd, 0)
    return fake_clone
