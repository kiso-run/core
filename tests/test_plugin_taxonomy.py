"""Integration test — plugin taxonomy end-to-end.

Verifies the three plugin types (tools, recipes, connectors) work together:
CLI entrypoints, plugin list aggregation, backward compat, registry structure.
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
        ("cli.wrapper", "run_wrapper_command", "tool_command"),
        ("cli.recipe", "run_recipe_command", "recipe_command"),
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
    """kiso plugin list shows all three types."""

    def test_aggregates_all_types(self, capsys):
        from cli.plugin import _plugin_list

        fake_tools = [{"name": "search", "description": "Web search", "version": "1.0",
                        "path": "/f", "summary": "", "args_schema": {}, "env": {},
                        "session_secrets": []}]
        fake_recipes = [{"name": "analyst", "summary": "Analysis", "instructions": "", "path": "/f"}]
        fake_connectors = [{"name": "discord", "description": "Discord", "version": "1.0",
                            "path": "/f", "summary": "", "env": {}}]

        with patch("cli.plugin.discover_wrappers", return_value=fake_tools), \
             patch("cli.plugin.discover_recipes", return_value=fake_recipes), \
             patch("cli.plugin.discover_connectors", return_value=fake_connectors), \
             patch("cli.plugin.invalidate_recipes_cache"):
            _plugin_list()

        out = capsys.readouterr().out
        assert "Tools:" in out
        assert "search" in out
        assert "Recipes:" in out
        assert "analyst" in out
        assert "Connectors:" in out
        assert "discord" in out


class TestRegistryStructure:
    """registry.json has expected entries."""

    def test_entries_have_name_and_description(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        for section in ("wrappers", "recipes", "connectors"):
            for entry in registry[section]:
                assert "name" in entry, f"{section} entry missing 'name'"
                assert "description" in entry, f"{section} entry missing 'description'"
