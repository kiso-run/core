"""M453: Tests for CLI plugin commands (unified plugin view)."""

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
        with patch("cli.plugin.discover_tools", return_value=_FAKE_TOOLS), \
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
        with patch("cli.plugin.discover_tools", return_value=_FAKE_TOOLS), \
             patch("cli.plugin.discover_recipes", return_value=[]), \
             patch("cli.plugin.discover_connectors", return_value=[]), \
             patch("cli.plugin.invalidate_recipes_cache"):
            _plugin_list()

        out = capsys.readouterr().out
        assert "Tools:" in out
        assert "Recipes:" not in out
        assert "Connectors:" not in out

    def test_list_empty(self, capsys):
        with patch("cli.plugin.discover_tools", return_value=[]), \
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
            "recipes": [],
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
        registry = {"tools": [], "recipes": [], "connectors": []}
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
