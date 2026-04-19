"""Integration test — plugin taxonomy end-to-end.

Verifies the two remaining plugin types (wrappers + connectors) still
surface through the ``kiso plugin`` umbrella. Recipes were retired in
M1504 part 2b (v0.10); wrappers follow in part 2c.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestPluginTaxonomyCLIEntrypoints:
    """All CLI entrypoints import and dispatch without errors."""

    @pytest.mark.parametrize("module,func,attr", [
        # M1504 retired cli.wrapper and cli.recipe. cli.connector is slated
        # for retirement in M1525; cli.plugin is the only surface left here.
        ("cli.connector", "run_connector_command", "connector_command"),
        ("cli.plugin", "run_plugin_command", "plugin_command"),
    ])
    def test_no_subcommand_exits(self, module, func, attr):
        import importlib
        mod = importlib.import_module(module)
        cmd = getattr(mod, func)

        class _Args:
            pass
        setattr(_Args, attr, None)

        with pytest.raises(SystemExit):
            cmd(_Args())


class TestPluginListAggregation:
    """kiso plugin list shows wrappers + connectors (recipes retired)."""

    def test_aggregates_wrappers_and_connectors(self, capsys):
        from cli.plugin import _plugin_list

        fake_tools = [{"name": "search", "description": "Web search", "version": "1.0",
                        "path": "/f", "summary": "", "args_schema": {}, "env": {},
                        "session_secrets": []}]
        fake_connectors = [{"name": "discord", "description": "Discord", "version": "1.0",
                            "path": "/f", "summary": "", "env": {}}]

        with patch("cli.plugin.discover_wrappers", return_value=fake_tools), \
             patch("cli.plugin.discover_connectors", return_value=fake_connectors):
            _plugin_list()

        out = capsys.readouterr().out
        assert "Wrappers:" in out
        assert "search" in out
        assert "Connectors:" in out
        assert "discord" in out
        assert "Recipes:" not in out


class TestRegistryStructure:
    """registry.json has expected entries for the types that still ship."""

    def test_entries_have_name_and_description(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        for section in ("wrappers", "connectors"):
            for entry in registry.get(section, []):
                assert "name" in entry, f"{section} entry missing 'name'"
                assert "description" in entry, f"{section} entry missing 'description'"
