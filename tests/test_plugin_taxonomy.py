"""M454: Integration test — plugin taxonomy end-to-end.

Verifies the three plugin types (tools, skills, connectors) work together:
CLI entrypoints, plugin list aggregation, backward compat, registry structure.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestPluginTaxonomyCLIEntrypoints:
    """All three CLI entrypoints import and dispatch without errors."""

    def test_tool_command_dispatch(self):
        from cli.tool import run_tool_command
        # No tool_command attr → prints usage and exits
        class _Args:
            tool_command = None
        with pytest.raises(SystemExit):
            run_tool_command(_Args())

    def test_skill_command_dispatch(self):
        from cli.skill import run_skill_command
        class _Args:
            skill_command = None
        with pytest.raises(SystemExit):
            run_skill_command(_Args())

    def test_connector_command_dispatch(self):
        from cli.connector import run_connector_command
        class _Args:
            connector_command = None
        with pytest.raises(SystemExit):
            run_connector_command(_Args())

    def test_plugin_command_dispatch(self):
        from cli.plugin import run_plugin_command
        class _Args:
            plugin_command = None
        with pytest.raises(SystemExit):
            run_plugin_command(_Args())


class TestPluginListAggregation:
    """kiso plugin list shows all three types."""

    def test_aggregates_all_types(self, capsys):
        from cli.plugin import _plugin_list

        fake_tools = [{"name": "search", "description": "Web search", "version": "1.0",
                        "path": "/f", "summary": "", "args_schema": {}, "env": {},
                        "session_secrets": []}]
        fake_skills = [{"name": "analyst", "summary": "Analysis", "instructions": "", "path": "/f"}]
        fake_connectors = [{"name": "discord", "description": "Discord", "version": "1.0",
                            "path": "/f", "summary": "", "env": {}}]

        with patch("cli.plugin.discover_tools", return_value=fake_tools), \
             patch("cli.plugin.discover_md_skills", return_value=fake_skills), \
             patch("cli.plugin.discover_connectors", return_value=fake_connectors), \
             patch("cli.plugin.invalidate_md_skills_cache"):
            _plugin_list()

        out = capsys.readouterr().out
        assert "Tools:" in out
        assert "search" in out
        assert "Skills:" in out
        assert "analyst" in out
        assert "Connectors:" in out
        assert "discord" in out


class TestBackwardCompat:
    """Old kiso.toml [kiso.skill] section still works."""

    def test_old_skill_section_loads(self):
        """kiso/tools.py reads both [kiso.tool] and [kiso.skill] from manifests."""
        from kiso.tools import _validate_manifest

        # Old-style manifest with [kiso.skill]
        old_manifest = {
            "kiso": {
                "name": "legacy-tool",
                "skill": {
                    "exec": "run.sh",
                    "description": "A legacy tool",
                },
            },
        }
        # Should not raise — backward compat reads "skill" key
        errors = _validate_manifest(old_manifest, Path("/fake"))
        assert not errors or all("exec" not in e for e in errors)

    def test_task_type_skill_is_alias(self):
        """TASK_TYPE_SKILL is an alias for TASK_TYPE_TOOL."""
        from kiso.brain import TASK_TYPE_SKILL, TASK_TYPE_TOOL
        assert TASK_TYPE_SKILL == TASK_TYPE_TOOL

    def test_task_types_include_both(self):
        """TASK_TYPES frozenset includes both 'tool' and 'skill'."""
        from kiso.brain import TASK_TYPES
        assert "tool" in TASK_TYPES
        assert "skill" in TASK_TYPES


class TestRegistryStructure:
    """registry.json has the expected keys."""

    def test_has_tools_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "tools" in registry
        assert isinstance(registry["tools"], list)

    def test_has_skills_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "skills" in registry
        assert isinstance(registry["skills"], list)

    def test_has_connectors_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "connectors" in registry
        assert isinstance(registry["connectors"], list)

    def test_entries_have_name_and_description(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        for section in ("tools", "skills", "connectors"):
            for entry in registry[section]:
                assert "name" in entry, f"{section} entry missing 'name'"
                assert "description" in entry, f"{section} entry missing 'description'"
