"""M188: Smoke test — tool install → broken deps → planner reinstall guidance."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from unittest.mock import MagicMock

from kiso.brain import validate_plan
from kiso.config import User


def _ok_run(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 0)


def _admin_cfg():
    cfg = MagicMock()
    cfg.users = {"alice": User(role="admin")}
    return cfg


@pytest.fixture()
def mock_admin():
    with (
        patch("cli.plugin_ops.load_config", return_value=_admin_cfg()),
        patch("cli.plugin_ops.getpass.getuser", return_value="alice"),
    ):
        yield


class TestToolInstallHealthSmoke:
    """End-to-end test: install tool with missing deps → check_deps catches it →
    planner tool list shows [BROKEN] → planner prompt has reinstall guidance →
    validation error for null args includes example format."""

    def test_install_detects_missing_binary(self, tmp_path, capsys, mock_admin):
        """M183/M187: _plugin_install passes deps from manifest to check_deps."""
        from cli.tool import _tool_install

        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        def fake_clone(cmd, **kwargs):
            dest = Path(cmd[3])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "kiso.toml").write_text(
                '[kiso]\ntype = "skill"\nname = "browser"\n'
                "[kiso.skill]\n"
                'summary = "Browser automation"\n'
                'usage_guide = "Use browser"\n'
                '[kiso.deps]\nbin = ["fake_binary"]\n'
            )
            (dest / "run.py").write_text("pass\n")
            (dest / "pyproject.toml").write_text("[project]\nname = 'browser'\n")
            return subprocess.CompletedProcess(cmd, 0)

        def run_dispatch(cmd, **kwargs):
            if cmd[0] == "git":
                return fake_clone(cmd, **kwargs)
            return _ok_run(cmd, **kwargs)

        captured_info = {}

        def spy_check_deps(info):
            captured_info.update(info)
            return ["fake_binary"]

        with (
            patch("cli.tool.TOOLS_DIR", tools_dir),
            patch("subprocess.run", side_effect=run_dispatch),
            patch("cli.tool.check_deps", side_effect=spy_check_deps),
        ):
            _tool_install(argparse.Namespace(
                target="browser", name=None, no_deps=False, show_deps=False,
            ))

        # check_deps received deps from manifest
        assert "deps" in captured_info
        assert captured_info["deps"] == {"bin": ["fake_binary"]}
        out = capsys.readouterr().out
        assert "fake_binary" in out

    def test_discover_tools_marks_unhealthy(self, tmp_path):
        """M176: discover_tools adds healthy=False for missing binary deps."""
        from kiso.tools import discover_tools

        tool_dir = tmp_path / "browser"
        tool_dir.mkdir()
        (tool_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "browser"\n'
            "[kiso.skill]\n"
            'summary = "Browser automation"\n'
            'usage_guide = "Use browser"\n'
            '[kiso.deps]\nbin = ["fake_binary"]\n'
        )
        (tool_dir / "run.py").write_text("pass\n")
        (tool_dir / "pyproject.toml").write_text("[project]\nname = 'browser'\n")

        tools = discover_tools(tmp_path)
        assert len(tools) == 1
        assert tools[0]["healthy"] is False
        assert "fake_binary" in tools[0]["missing_deps"]

    def test_planner_tool_list_shows_broken(self, tmp_path):
        """M177: build_planner_tool_list annotates unhealthy tools."""
        from kiso.tools import build_planner_tool_list, discover_tools

        tool_dir = tmp_path / "browser"
        tool_dir.mkdir()
        (tool_dir / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "browser"\n'
            "[kiso.skill]\n"
            'summary = "Browser automation"\n'
            'usage_guide = "Use browser"\n'
            '[kiso.deps]\nbin = ["fake_binary"]\n'
        )
        (tool_dir / "run.py").write_text("pass\n")
        (tool_dir / "pyproject.toml").write_text("[project]\nname = 'browser'\n")

        tools = discover_tools(tmp_path)
        result = build_planner_tool_list(tools)
        assert "[BROKEN" in result
        assert "fake_binary" in result
        assert "kiso tool remove" in result

    def test_planner_prompt_has_reinstall_guidance(self):
        """M185: planner prompt blocks apt-get and has reinstall rule."""
        prompt = (Path(__file__).parent.parent / "kiso" / "roles" / "planner.md").read_text()
        assert "NEVER use `apt-get install`" in prompt or "Never jump to `apt-get install`" in prompt
        assert ("kiso tool remove NAME && kiso tool install NAME" in prompt
                or "kiso skill remove NAME && kiso skill install NAME" in prompt)

    def test_validation_error_includes_args_example(self):
        """M184: validation error for null args includes example format."""
        plan = {"tasks": [
            {"type": "skill", "detail": "screenshot", "skill": "browser",
             "args": None, "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        info = {"browser": {"args_schema": {
            "action": {"type": "string", "required": True},
        }}}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert len(errors) == 1
        assert "Set args to a JSON string like:" in errors[0]
        assert '"action": "value"' in errors[0]
