"""Tests for CLI plugin commands (unified plugin view)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli.plugin import _plugin_list, _plugin_search, run_plugin_command


class _FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


_FAKE_CONNECTORS = [
    {"name": "discord", "description": "Discord bridge", "version": "1.0",
     "path": "/fake", "summary": "", "env": {}},
]


class TestPluginList:
    def test_list_connectors(self, capsys):
        with patch("cli.plugin.discover_connectors", return_value=_FAKE_CONNECTORS):
            _plugin_list()

        out = capsys.readouterr().out
        assert "Connectors:" in out
        assert "discord" in out
        # Recipes retired in the v0.10 cycle — section must not surface
        assert "Recipes:" not in out

    def test_list_empty(self, capsys):
        with patch("cli.plugin.discover_connectors", return_value=[]):
            _plugin_list()

        out = capsys.readouterr().out
        assert "No plugins installed" in out


class TestPluginSearch:
    def test_search_connectors(self, capsys):
        registry = {
            "connectors": [{"name": "discord", "description": "Discord bridge"}],
        }
        args = _FakeArgs(query="")
        with patch("cli.plugin.fetch_registry", return_value=registry):
            _plugin_search(args)

        out = capsys.readouterr().out
        assert "Connectors:" in out
        assert "discord" in out

    def test_search_no_results(self, capsys):
        registry = {"connectors": []}
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
