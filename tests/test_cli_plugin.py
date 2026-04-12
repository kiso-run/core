"""Tests for CLI plugin commands (unified plugin view)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli.plugin import _plugin_list, _plugin_search, run_plugin_command


class _FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


_FAKE_TOOLS = [
    {"name": "browser", "description": "Headless browser automation", "version": "1.0",
     "path": "/fake", "summary": "", "args_schema": {}, "env": {}, "session_secrets": []},
    {"name": "search", "description": "Web search", "version": "1.0",
     "path": "/fake", "summary": "", "args_schema": {}, "env": {}, "session_secrets": []},
]

_FAKE_RECIPES = [
    {"name": "data-analyst", "summary": "Data analysis guidance",
     "instructions": "Use pandas.", "path": "/fake/data-analyst.md"},
]

_FAKE_CONNECTORS = [
    {"name": "discord", "description": "Discord bridge", "version": "1.0",
     "path": "/fake", "summary": "", "env": {}},
]


class TestPluginList:
    def test_list_all_types(self, capsys):
        with patch("cli.plugin.discover_wrappers", return_value=_FAKE_TOOLS), \
             patch("cli.plugin.discover_recipes", return_value=_FAKE_RECIPES), \
             patch("cli.plugin.discover_connectors", return_value=_FAKE_CONNECTORS), \
             patch("cli.plugin.invalidate_recipes_cache"):
            _plugin_list()

        out = capsys.readouterr().out
        assert "Tools:" in out
        assert "browser" in out
        assert "Recipes:" in out
        assert "data-analyst" in out
        assert "Connectors:" in out
        assert "discord" in out

    def test_list_tools_only(self, capsys):
        with patch("cli.plugin.discover_wrappers", return_value=_FAKE_TOOLS), \
             patch("cli.plugin.discover_recipes", return_value=[]), \
             patch("cli.plugin.discover_connectors", return_value=[]), \
             patch("cli.plugin.invalidate_recipes_cache"):
            _plugin_list()

        out = capsys.readouterr().out
        assert "Tools:" in out
        assert "Recipes:" not in out
        assert "Connectors:" not in out

    def test_list_empty(self, capsys):
        with patch("cli.plugin.discover_wrappers", return_value=[]), \
             patch("cli.plugin.discover_recipes", return_value=[]), \
             patch("cli.plugin.discover_connectors", return_value=[]), \
             patch("cli.plugin.invalidate_recipes_cache"):
            _plugin_list()

        out = capsys.readouterr().out
        assert "No plugins installed" in out


class TestPluginSearch:
    def test_search_across_types(self, capsys):
        registry = {
            "tools": [{"name": "browser", "description": "Browser automation"}],
            "exclude_recipes": [],
            "connectors": [{"name": "discord", "description": "Discord bridge"}],
        }
        args = _FakeArgs(query="")
        with patch("cli.plugin.fetch_registry", return_value=registry):
            _plugin_search(args)

        out = capsys.readouterr().out
        assert "Tools:" in out
        assert "browser" in out
        assert "Connectors:" in out
        assert "discord" in out

    def test_search_no_results(self, capsys):
        registry = {"tools": [], "exclude_recipes": [], "connectors": []}
        args = _FakeArgs(query="nonexistent")
        with patch("cli.plugin.fetch_registry", return_value=registry):
            _plugin_search(args)

        out = capsys.readouterr().out
        assert "No plugins found" in out


class TestRunPluginCommand:
    def test_no_command(self):
        args = _FakeArgs(plugin_command=None)
        with pytest.raises(SystemExit):
            run_plugin_command(args)


class TestPluginInstallGitPull:
    """kiso wrapper install runs git pull when plugin already exists."""

    def test_git_pull_called_on_reinstall(self, tmp_path):
        from cli.plugin_ops import _plugin_install
        from kiso.wrappers import _validate_manifest as validate_fn, check_deps

        # Create a fake installed plugin dir with kiso.toml
        plugin_dir = tmp_path / "wrappers" / "test-tool"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "kiso.toml").write_text(
            '[kiso]\nname = "test-tool"\ntype = "wrapper"\n\n'
            '[kiso.wrapper]\nsummary = "Test"\n\n'
            '[kiso.tool.args]\n'
        )

        calls = []

        def _mock_run(cmd, **kw):
            calls.append(cmd)
            result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return result

        args = type("A", (), {"no_deps": True, "target": "test-tool", "name": None, "show_deps": False})()

        with patch("cli.plugin_ops.subprocess.run", side_effect=_mock_run):
            _plugin_install(
                "tool", "tool-",
                tmp_path / "wrappers",
                validate_fn, check_deps,
                args,
            )

        # git pull should be called before uv sync
        git_calls = [c for c in calls if c[0] == "git"]
        assert any("pull" in c for c in git_calls), f"Expected git pull, got: {calls}"
