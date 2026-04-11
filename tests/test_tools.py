"""Tests for kiso/tools.py — tool discovery, validation, and execution."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.wrappers import (
    MAX_ARGS_DEPTH,
    MAX_ARGS_SIZE,
    _check_args_depth,
    _coerce_value,
    _env_var_name,
    _load_wrapper_validator,
    _validate_manifest,
    build_planner_wrapper_list,
    build_wrapper_env,
    build_wrapper_input,
    check_deps,
    discover_wrappers,
    invalidate_wrappers_cache,
    auto_correct_wrapper_args,
    repair_wrapper_args,
    validate_wrapper_args,
    validate_wrapper_args_semantic,
)
from kiso.plugins import _validate_plugin_manifest_base


# --- Helpers ---

MINIMAL_TOML = """\
[kiso]
type = "tool"
name = "echo"
version = "0.1.0"
description = "Echo tool"

[kiso.tool]
summary = "Echoes input back"
usage_guide = "Just pass any text."

[kiso.tool.args]
text = { type = "string", required = true, description = "text to echo" }
"""

FULL_TOML = """\
[kiso]
type = "tool"
name = "search"
version = "0.2.0"
description = "Web search"

[kiso.tool]
summary = "Web search via API"
usage_guide = "Use short queries. Prefer English keywords."
session_secrets = ["api_token"]

[kiso.tool.args]
query = { type = "string", required = true, description = "search query" }
max_results = { type = "int", required = false, default = 5, description = "max results" }

[kiso.tool.env]
api_key = { required = true }

[kiso.deps]
python = ">=3.11"
bin = ["curl"]
"""


def _create_tool(tmp_path: Path, name: str, toml_content: str) -> Path:
    """Create a tool directory with kiso.toml, run.py, pyproject.toml."""
    tool_dir = tmp_path / name
    tool_dir.mkdir()
    (tool_dir / "kiso.toml").write_text(toml_content)
    (tool_dir / "run.py").write_text("import json, sys\ndata = json.load(sys.stdin)\nprint(data['args'].get('text', 'ok'))")
    (tool_dir / "pyproject.toml").write_text("[project]\nname = \"test\"\nversion = \"0.1.0\"")
    return tool_dir


def _write_validator(tool_dir: Path, body: str) -> None:
    """Write a validator.py file for a fake tool."""
    (tool_dir / "validator.py").write_text(body)


# --- _validate_plugin_manifest_base ---

class TestValidatePluginManifestBase:
    def test_missing_kiso_section(self, tmp_path):
        errors = _validate_plugin_manifest_base({}, tmp_path, "tool")
        assert "missing [kiso] section" in errors

    def test_wrong_type(self, tmp_path):
        manifest = {"kiso": {"type": "connector", "name": "x", "tool": {}}}
        (tmp_path / "run.py").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        errors = _validate_plugin_manifest_base(manifest, tmp_path, "tool")
        assert any("kiso.type must be 'tool'" in e for e in errors)

    def test_missing_plugin_section(self, tmp_path):
        manifest = {"kiso": {"type": "tool", "name": "x"}}
        errors = _validate_plugin_manifest_base(manifest, tmp_path, "tool")
        assert "missing [kiso.tool] section" in errors

    def test_missing_run_py(self, tmp_path):
        manifest = {"kiso": {"type": "tool", "name": "x", "tool": {}}}
        (tmp_path / "pyproject.toml").write_text("")
        errors = _validate_plugin_manifest_base(manifest, tmp_path, "tool")
        assert "run.py is missing" in errors

    def test_missing_pyproject(self, tmp_path):
        manifest = {"kiso": {"type": "tool", "name": "x", "tool": {}}}
        (tmp_path / "run.py").write_text("")
        errors = _validate_plugin_manifest_base(manifest, tmp_path, "tool")
        assert "pyproject.toml is missing" in errors

    def test_valid_returns_empty(self, tmp_path):
        manifest = {"kiso": {"type": "connector", "name": "x", "connector": {}}}
        (tmp_path / "run.py").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        errors = _validate_plugin_manifest_base(manifest, tmp_path, "connector")
        assert errors == []

    def test_connector_type_used_in_messages(self, tmp_path):
        manifest = {"kiso": {"type": "tool", "name": "x", "connector": {}}}
        errors = _validate_plugin_manifest_base(manifest, tmp_path, "connector")
        assert any("'connector'" in e for e in errors)


# --- _validate_manifest ---

class TestValidateManifest:
    def test_valid_minimal(self, tmp_path):
        _create_tool(tmp_path, "echo", MINIMAL_TOML)
        import tomllib
        with open(tmp_path / "echo" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "echo")
        assert errors == []

    def test_missing_kiso_section(self, tmp_path):
        _create_tool(tmp_path, "bad", "[other]\nfoo = 1\n")
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert "missing [kiso] section" in errors

    def test_wrong_type(self, tmp_path):
        toml = MINIMAL_TOML.replace('type = "tool"', 'type = "connector"')
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("kiso.type must be 'tool'" in e for e in errors)

    def test_missing_name(self, tmp_path):
        toml = MINIMAL_TOML.replace('name = "echo"', '')
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("kiso.name is required" in e for e in errors)

    def test_missing_tool_section(self, tmp_path):
        toml = "[kiso]\ntype = \"tool\"\nname = \"x\"\n"
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert "missing [kiso.tool] section" in errors

    def test_missing_summary(self, tmp_path):
        toml = MINIMAL_TOML.replace('summary = "Echoes input back"', '')
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("kiso.tool.summary is required" in e for e in errors)

    def test_invalid_arg_type(self, tmp_path):
        toml = MINIMAL_TOML.replace('type = "string"', 'type = "date"')
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("'date'" in e for e in errors)

    def test_missing_run_py(self, tmp_path):
        tool_dir = tmp_path / "bad"
        tool_dir.mkdir()
        (tool_dir / "kiso.toml").write_text(MINIMAL_TOML)
        (tool_dir / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0.1.0\"")
        import tomllib
        with open(tool_dir / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tool_dir)
        assert "run.py is missing" in errors

    def test_missing_pyproject(self, tmp_path):
        tool_dir = tmp_path / "bad"
        tool_dir.mkdir()
        (tool_dir / "kiso.toml").write_text(MINIMAL_TOML)
        (tool_dir / "run.py").write_text("pass")
        import tomllib
        with open(tool_dir / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tool_dir)
        assert "pyproject.toml is missing" in errors

    def test_args_not_a_table(self, tmp_path):
        toml = (
            '[kiso]\ntype = "tool"\nname = "x"\n'
            '[kiso.tool]\nsummary = "X"\nargs = "not_a_table"\n'
        )
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("[kiso.tool.args] must be a table" in e for e in errors)

    def test_arg_not_a_table(self, tmp_path):
        toml = MINIMAL_TOML.replace(
            'text = { type = "string", required = true, description = "text to echo" }',
            'text = "not_a_table"',
        )
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("arg 'text' must be a table" in e for e in errors)

    def test_env_not_a_table(self, tmp_path):
        toml = (
            '[kiso]\ntype = "tool"\nname = "x"\n'
            '[kiso.tool]\nsummary = "X"\nenv = "not_a_table"\n'
        )
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("[kiso.tool.env] must be a table" in e for e in errors)

    def test_invalid_session_secrets_type(self, tmp_path):
        toml = MINIMAL_TOML.replace(
            "[kiso.tool.args]",
            'session_secrets = "not_a_list"\n\n[kiso.tool.args]',
        )
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("session_secrets must be a list" in e for e in errors)

    def test_usage_guide_valid_string(self, tmp_path):
        _create_tool(tmp_path, "ok", MINIMAL_TOML)
        import tomllib
        with open(tmp_path / "ok" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "ok")
        assert errors == []

    def test_usage_guide_not_string(self, tmp_path):
        toml = MINIMAL_TOML.replace(
            'usage_guide = "Just pass any text."',
            "usage_guide = 42",
        )
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("usage_guide is required" in e for e in errors)

    def test_usage_guide_absent(self, tmp_path):
        """usage_guide is required — absence causes an error."""
        toml = MINIMAL_TOML.replace('usage_guide = "Just pass any text."\n', "")
        _create_tool(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("usage_guide is required" in e for e in errors)


# --- _env_var_name ---

class TestEnvVarName:
    def test_basic(self):
        assert _env_var_name("search", "api_key") == "KISO_WRAPPER_SEARCH_API_KEY"

    def test_with_dashes(self):
        assert _env_var_name("my-tool", "auth-token") == "KISO_WRAPPER_MY_TOOL_AUTH_TOKEN"

    def test_uppercase(self):
        assert _env_var_name("Echo", "Key") == "KISO_WRAPPER_ECHO_KEY"


# --- discover_wrappers ---

class TestDiscoverTools:
    def test_empty_dir(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        result = discover_wrappers(tools_dir)
        assert result == []

    def test_nonexistent_dir(self, tmp_path):
        result = discover_wrappers(tmp_path / "nonexistent")
        assert result == []

    def test_nonexistent_dir_logs_warning(self, tmp_path, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="kiso.wrappers"):
            discover_wrappers(tmp_path / "missing_tools")
        assert "Tools directory not found" in caplog.text

    def test_discovers_valid_tool(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)
        result = discover_wrappers(tools_dir)
        assert len(result) == 1
        assert result[0]["name"] == "echo"
        assert result[0]["summary"] == "Echoes input back"
        assert "text" in result[0]["args_schema"]

    def test_skips_installing(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        tool_dir = _create_tool(tools_dir, "echo", MINIMAL_TOML)
        (tool_dir / ".installing").touch()
        result = discover_wrappers(tools_dir)
        assert result == []

    def test_skips_no_kiso_toml(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        (tools_dir / "empty").mkdir()
        result = discover_wrappers(tools_dir)
        assert result == []

    def test_logs_scan_path_and_found_tools(self, tmp_path, caplog):
        """discover_wrappers logs the scanned path and found tool names."""
        import logging
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)
        with caplog.at_level(logging.DEBUG, logger="kiso.wrappers"):
            discover_wrappers(tools_dir)
        assert f"scanning {tools_dir}" in caplog.text
        assert "found 1 tools: echo" in caplog.text

    def test_logs_empty_scan_with_subdirs(self, tmp_path, caplog):
        """discover_wrappers logs subdirectory names when 0 tools found."""
        import logging
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        (tools_dir / "broken").mkdir()  # no kiso.toml → skipped
        with caplog.at_level(logging.DEBUG, logger="kiso.wrappers"):
            discover_wrappers(tools_dir)
        assert "0 tools found" in caplog.text
        assert "broken" in caplog.text

    def test_skips_invalid_manifest(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        bad_dir = tools_dir / "bad"
        bad_dir.mkdir()
        (bad_dir / "kiso.toml").write_text("[kiso]\ntype = \"connector\"\n")
        (bad_dir / "run.py").write_text("pass")
        (bad_dir / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0.1.0\"")
        result = discover_wrappers(tools_dir)
        assert result == []

    def test_skips_corrupt_toml(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        bad_dir = tools_dir / "corrupt"
        bad_dir.mkdir()
        (bad_dir / "kiso.toml").write_text("this is not valid toml {{{{")
        (bad_dir / "run.py").write_text("pass")
        (bad_dir / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0.1.0\"")
        result = discover_wrappers(tools_dir)
        assert result == []

    def test_multiple_tools_sorted(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "beta", MINIMAL_TOML.replace('name = "echo"', 'name = "beta"'))
        _create_tool(tools_dir, "alpha", MINIMAL_TOML.replace('name = "echo"', 'name = "alpha"'))
        result = discover_wrappers(tools_dir)
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "beta"

    def test_skips_files_not_dirs(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        (tools_dir / "random_file.txt").touch()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)
        result = discover_wrappers(tools_dir)
        assert len(result) == 1

    def test_full_tool_fields(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "search", FULL_TOML)
        result = discover_wrappers(tools_dir)
        assert len(result) == 1
        t = result[0]
        assert t["name"] == "search"
        assert t["version"] == "0.2.0"
        assert t["description"] == "Web search"
        assert t["session_secrets"] == ["api_token"]
        assert "api_key" in t["env"]
        assert "query" in t["args_schema"]
        assert "max_results" in t["args_schema"]

    def test_duplicate_wrapper_name_skipped(self, tmp_path):
        """Two dirs with same kiso.name → only first returned."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "alpha-echo", MINIMAL_TOML)
        _create_tool(tools_dir, "beta-echo", MINIMAL_TOML)
        result = discover_wrappers(tools_dir)
        assert len(result) == 1
        assert result[0]["name"] == "echo"
        # Should come from alpha-echo (sorted first)
        assert "alpha-echo" in result[0]["path"]

    def test_discover_usage_guide_from_toml(self, tmp_path):
        """usage_guide from toml is returned when no local override."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)
        result = discover_wrappers(tools_dir)
        assert result[0]["usage_guide"] == "Just pass any text."

    def test_discover_usage_guide_override_file(self, tmp_path):
        """Local override file takes priority over toml default."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        tool_dir = _create_tool(tools_dir, "echo", MINIMAL_TOML)
        (tool_dir / "usage_guide.local.md").write_text("My custom guide\n")
        result = discover_wrappers(tools_dir)
        assert result[0]["usage_guide"] == "My custom guide"


# --- check_deps ---

class TestCheckDeps:
    def test_no_deps(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)
        tool = discover_wrappers(tools_dir)[0]
        result = check_deps(tool)
        assert result == []

    def test_existing_bin(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "search", FULL_TOML)
        tool = discover_wrappers(tools_dir)[0]
        # curl should be available in most test environments
        result = check_deps(tool)
        # Don't assert empty — curl might not be installed, just ensure it returns a list
        assert isinstance(result, list)

    def test_missing_bin(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        toml = FULL_TOML.replace('bin = ["curl"]', 'bin = ["nonexistent_binary_xyz"]')
        _create_tool(tools_dir, "search", toml)
        tool = discover_wrappers(tools_dir)[0]
        result = check_deps(tool)
        assert "nonexistent_binary_xyz" in result

    def test_bin_not_a_list(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        toml = FULL_TOML.replace('bin = ["curl"]', 'bin = "curl"')
        _create_tool(tools_dir, "search", toml)
        tool = discover_wrappers(tools_dir)[0]
        result = check_deps(tool)
        assert result == []

    def test_no_file_io_after_discover(self, tmp_path):
        """check_deps must not re-read kiso.toml; it uses tool['deps'] from discover_wrappers."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        toml = FULL_TOML.replace('bin = ["curl"]', 'bin = ["nonexistent_binary_xyz"]')
        tool_dir = _create_tool(tools_dir, "search", toml)
        tool = discover_wrappers(tools_dir)[0]

        # Delete kiso.toml — check_deps must still work from in-memory deps
        (tool_dir / "kiso.toml").unlink()
        result = check_deps(tool)
        assert "nonexistent_binary_xyz" in result

    def test_venv_bin_found(self, tmp_path):
        """check_deps finds binaries in the tool's .venv/bin/ directory."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        toml = FULL_TOML.replace('bin = ["curl"]', 'bin = ["myfakebin"]')
        tool_dir = _create_tool(tools_dir, "search", toml)
        tool = discover_wrappers(tools_dir)[0]

        # Binary not on system PATH
        assert check_deps(tool) == ["myfakebin"]

        # Create it in tool's .venv/bin/
        venv_bin = tool_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_bin = venv_bin / "myfakebin"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)

        # Now check_deps should find it
        assert check_deps(tool) == []

    def test_discover_includes_deps_key(self, tmp_path):
        """discover_wrappers must include 'deps' key with the manifest's [kiso.deps] section."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "search", FULL_TOML)
        tool = discover_wrappers(tools_dir)[0]
        assert "deps" in tool
        assert tool["deps"].get("bin") == ["curl"]

    def test_discover_deps_empty_when_absent(self, tmp_path):
        """discover_wrappers returns empty deps dict when [kiso.deps] is not in kiso.toml."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)
        tool = discover_wrappers(tools_dir)[0]
        assert tool["deps"] == {}

    def test_discover_healthy_when_deps_present(self, tmp_path):
        """Tool with all binary deps present is healthy=True."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        # FULL_TOML requires bin=["curl"] — curl should be available in test env
        _create_tool(tools_dir, "search", FULL_TOML)
        tool = discover_wrappers(tools_dir)[0]
        assert tool["healthy"] is True
        assert tool["missing_deps"] == []

    def test_discover_unhealthy_when_deps_missing(self, tmp_path):
        """Tool with missing binary deps is healthy=False."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        toml = FULL_TOML.replace('bin = ["curl"]', 'bin = ["nonexistent_binary_xyz_12345"]')
        _create_tool(tools_dir, "search", toml)
        tool = discover_wrappers(tools_dir)[0]
        assert tool["healthy"] is False
        assert "nonexistent_binary_xyz_12345" in tool["missing_deps"]

    def test_discover_healthy_no_deps_declared(self, tmp_path):
        """Tool with no [kiso.deps].bin is healthy=True."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)
        tool = discover_wrappers(tools_dir)[0]
        assert tool["healthy"] is True
        assert tool["missing_deps"] == []


# --- build_planner_wrapper_list ---

class TestBuildPlannerToolList:
    def _make_tool(self, name="echo", summary="Echo tool", args_schema=None):
        return {
            "name": name,
            "summary": summary,
            "args_schema": args_schema or {"text": {"type": "string", "required": True, "description": "input text"}},
            "env": {},
            "session_secrets": [],
            "path": "/fake",
            "version": "0.1.0",
            "description": "",
        }

    def test_empty_tools(self):
        assert build_planner_wrapper_list([]) == ""

    def test_admin_sees_all(self):
        tools = [self._make_tool("a", "Tool A"), self._make_tool("b", "Tool B")]
        result = build_planner_wrapper_list(tools, "admin")
        assert "- a — Tool A" in result
        assert "- b — Tool B" in result

    def test_user_star_sees_all(self):
        tools = [self._make_tool("a", "Tool A")]
        result = build_planner_wrapper_list(tools, "user", "*")
        assert "- a — Tool A" in result

    def test_user_list_filters(self):
        tools = [self._make_tool("a", "Tool A"), self._make_tool("b", "Tool B")]
        result = build_planner_wrapper_list(tools, "user", ["a"])
        assert "- a — Tool A" in result
        assert "- b — Tool B" not in result

    def test_user_empty_list(self):
        tools = [self._make_tool("a", "Tool A")]
        result = build_planner_wrapper_list(tools, "user", [])
        assert result == ""

    def test_user_none_tools(self):
        tools = [self._make_tool("a", "Tool A")]
        result = build_planner_wrapper_list(tools, "user", None)
        assert result == ""

    def test_includes_args_schema(self):
        schema = {
            "query": {"type": "string", "required": True, "description": "search query"},
            "limit": {"type": "int", "required": False, "default": 5, "description": "max results"},
        }
        tools = [self._make_tool("search", "Search", schema)]
        result = build_planner_wrapper_list(tools, "admin")
        assert "query (string, required): search query" in result
        assert "limit (int, optional, default=5): max results" in result

    def test_header_present(self):
        tools = [self._make_tool()]
        result = build_planner_wrapper_list(tools, "admin")
        assert result.startswith("Available tools:")

    def test_planner_list_includes_guide(self):
        tool = self._make_tool()
        tool["usage_guide"] = "Use short queries"
        result = build_planner_wrapper_list([tool], "admin")
        assert "guide: Use short queries" in result

    def test_planner_list_no_guide(self):
        tool = self._make_tool()
        tool["usage_guide"] = ""
        result = build_planner_wrapper_list([tool], "admin")
        assert "guide:" not in result

    def test_optional_args_trimmed(self):
        """only top 3 optional args shown, rest counted."""
        schema = {
            "url": {"type": "string", "required": True, "description": "target URL"},
            "opt_a": {"type": "string", "required": False, "description": "optional A"},
            "opt_b": {"type": "string", "required": False, "description": "optional B"},
            "opt_c": {"type": "string", "required": False, "description": "optional C"},
            "opt_d": {"type": "string", "required": False, "description": "optional D"},
            "opt_e": {"type": "string", "required": False, "description": "optional E"},
        }
        tools = [self._make_tool("browser", "Browser tool", schema)]
        result = build_planner_wrapper_list(tools, "admin")
        assert "url (string, required)" in result
        assert "opt_a" in result
        assert "opt_b" in result
        assert "opt_c" in result
        assert "opt_d" not in result
        assert "opt_e" not in result
        assert "(2 more optional args)" in result

    def test_few_optional_args_no_trim(self):
        """3 or fewer optional args → no trim message."""
        schema = {
            "url": {"type": "string", "required": True, "description": "target URL"},
            "opt_a": {"type": "string", "required": False, "description": "optional A"},
        }
        tools = [self._make_tool("browser", "Browser tool", schema)]
        result = build_planner_wrapper_list(tools, "admin")
        assert "opt_a" in result
        assert "more optional" not in result

    def test_unhealthy_tool_shows_broken_annotation(self):
        tool = self._make_tool("browser", "Browser automation")
        tool["healthy"] = False
        tool["missing_deps"] = ["playwright"]
        result = build_planner_wrapper_list([tool], "admin")
        assert "[BROKEN" in result
        assert "missing: playwright" in result
        assert "kiso tool remove browser" in result
        assert "kiso tool install browser" in result

    def test_healthy_tool_no_broken_annotation(self):
        tool = self._make_tool("browser", "Browser automation")
        tool["healthy"] = True
        tool["missing_deps"] = []
        result = build_planner_wrapper_list([tool], "admin")
        assert "- browser — Browser automation" in result
        assert "[BROKEN" not in result

    def test_mixed_healthy_and_unhealthy(self):
        healthy = self._make_tool("echo", "Echo tool")
        healthy["healthy"] = True
        healthy["missing_deps"] = []
        broken = self._make_tool("browser", "Browser automation")
        broken["healthy"] = False
        broken["missing_deps"] = ["playwright", "chromium"]
        result = build_planner_wrapper_list([healthy, broken], "admin")
        assert "- echo — Echo tool" in result
        assert "[BROKEN" in result
        assert "playwright, chromium" in result


# --- build_planner_wrapper_list selected_names ---


class TestBuildPlannerToolListSelectedNames:
    def test_selected_gets_full_guide(self):
        tools = [{"name": "browser", "summary": "Browse", "args_schema": {}, "usage_guide": "Full guide here", "healthy": True, "consumes": []}]
        result = build_planner_wrapper_list(tools, selected_names={"browser"})
        assert "Full guide here" in result

    def test_non_selected_omits_guide(self):
        tools = [{"name": "browser", "summary": "Browse", "args_schema": {}, "usage_guide": "Full guide here", "healthy": True, "consumes": []}]
        result = build_planner_wrapper_list(tools, selected_names={"other"})
        assert "Full guide here" not in result
        assert "browser" in result  # still listed

    def test_none_selected_includes_all_guides(self):
        tools = [{"name": "browser", "summary": "Browse", "args_schema": {}, "usage_guide": "Full guide", "healthy": True, "consumes": []}]
        result = build_planner_wrapper_list(tools, selected_names=None)
        assert "Full guide" in result

    def test_mixed_selection(self):
        tools = [
            {"name": "browser", "summary": "Browse", "args_schema": {}, "usage_guide": "Browser guide", "healthy": True, "consumes": []},
            {"name": "aider", "summary": "Code", "args_schema": {}, "usage_guide": "Aider guide", "healthy": True, "consumes": []},
        ]
        result = build_planner_wrapper_list(tools, selected_names={"browser"})
        assert "Browser guide" in result
        assert "Aider guide" not in result
        assert "aider" in result  # name still listed


# --- validate_wrapper_args ---

class TestValidateToolArgs:
    SCHEMA = {
        "query": {"type": "string", "required": True},
        "limit": {"type": "int", "required": False},
        "ratio": {"type": "float", "required": False},
        "verbose": {"type": "bool", "required": False},
    }

    def test_valid_required_only(self):
        errors = validate_wrapper_args({"query": "test"}, self.SCHEMA)
        assert errors == []

    def test_valid_all_args(self):
        errors = validate_wrapper_args(
            {"query": "test", "limit": 10, "ratio": 0.5, "verbose": True},
            self.SCHEMA,
        )
        assert errors == []

    def test_missing_required(self):
        errors = validate_wrapper_args({}, self.SCHEMA)
        assert any("missing required arg: query" in e for e in errors)

    def test_wrong_type_string(self):
        errors = validate_wrapper_args({"query": 123}, self.SCHEMA)
        assert any("expected string" in e for e in errors)

    def test_wrong_type_int(self):
        errors = validate_wrapper_args({"query": "ok", "limit": "ten"}, self.SCHEMA)
        assert any("expected int" in e for e in errors)

    def test_bool_not_int(self):
        errors = validate_wrapper_args({"query": "ok", "limit": True}, self.SCHEMA)
        assert any("expected int, got bool" in e for e in errors)

    def test_bool_not_float(self):
        errors = validate_wrapper_args({"query": "ok", "ratio": True}, self.SCHEMA)
        assert any("expected float, got bool" in e for e in errors)

    def test_int_as_float(self):
        errors = validate_wrapper_args({"query": "ok", "ratio": 5}, self.SCHEMA)
        assert errors == []  # int is valid as float

    def test_wrong_type_bool(self):
        errors = validate_wrapper_args({"query": "ok", "verbose": "yes"}, self.SCHEMA)
        assert any("expected bool" in e for e in errors)

    def test_unknown_args_allowed(self):
        errors = validate_wrapper_args({"query": "ok", "extra": "fine"}, self.SCHEMA)
        assert errors == []

    def test_max_size_exceeded(self):
        big_value = "x" * (MAX_ARGS_SIZE + 1)
        errors = validate_wrapper_args({"query": big_value}, self.SCHEMA)
        assert any("exceeds" in e for e in errors)

    def test_max_depth_exceeded(self):
        # Build nested dict exceeding MAX_ARGS_DEPTH
        nested: dict = {"query": "ok"}
        current = nested
        for _ in range(MAX_ARGS_DEPTH + 2):
            current["nested"] = {}
            current = current["nested"]
        errors = validate_wrapper_args(nested, self.SCHEMA)
        assert any("depth" in e for e in errors)

    def test_empty_args_empty_schema(self):
        errors = validate_wrapper_args({}, {})
        assert errors == []


class TestToolSemanticValidationHooks:
    def test_no_validator_fallback(self, tmp_path):
        _create_tool(tmp_path, "echo", MINIMAL_TOML)
        tool = discover_wrappers(tmp_path)[0]

        assert _load_wrapper_validator(tool) is None
        assert validate_wrapper_args_semantic(tool, {"text": "hi"}) == []
        assert repair_wrapper_args(tool, {"text": "hi"}) == {"text": "hi"}

    def test_validator_errors_are_returned(self, tmp_path):
        tool_dir = _create_tool(tmp_path, "echo", MINIMAL_TOML)
        _write_validator(
            tool_dir,
            "def validate_args(args, context):\n"
            "    if args.get('text') == 'bad':\n"
            "        return ['text is semantically invalid']\n"
            "    return []\n",
        )
        invalidate_wrappers_cache()
        tool = discover_wrappers(tmp_path)[0]

        errors = validate_wrapper_args_semantic(tool, {"text": "bad"}, {"phase": "planner"})

        assert errors == ["text is semantically invalid"]

    def test_validator_repair_is_applied_conservatively(self, tmp_path):
        tool_dir = _create_tool(tmp_path, "echo", MINIMAL_TOML)
        _write_validator(
            tool_dir,
            "def repair_args(args, context):\n"
            "    repaired = dict(args)\n"
            "    if isinstance(repaired.get('text'), str):\n"
            "        repaired['text'] = repaired['text'].strip()\n"
            "    return repaired\n",
        )
        invalidate_wrappers_cache()
        tool = discover_wrappers(tmp_path)[0]

        repaired = repair_wrapper_args(tool, {"text": "  hi  "}, {"phase": "worker"})

        assert repaired == {"text": "hi"}

    def test_validator_load_is_cached(self, tmp_path):
        tool_dir = _create_tool(tmp_path, "echo", MINIMAL_TOML)
        _write_validator(
            tool_dir,
            "def validate_args(args, context):\n"
            "    return []\n",
        )
        invalidate_wrappers_cache()
        tool = discover_wrappers(tmp_path)[0]

        with patch(
            "kiso.wrappers.importlib.util.spec_from_file_location",
            wraps=importlib.util.spec_from_file_location,
        ) as mock_spec:
            assert _load_wrapper_validator(tool) is not None
            assert _load_wrapper_validator(tool) is not None

        assert mock_spec.call_count == 1

    def test_invalid_validator_result_degrades_gracefully(self, tmp_path):
        tool_dir = _create_tool(tmp_path, "echo", MINIMAL_TOML)
        _write_validator(
            tool_dir,
            "def validate_args(args, context):\n"
            "    return 'not-a-list'\n"
            "def repair_args(args, context):\n"
            "    return 'not-a-dict'\n",
        )
        invalidate_wrappers_cache()
        tool = discover_wrappers(tmp_path)[0]

        assert validate_wrapper_args_semantic(tool, {"text": "hi"}) == []
        assert repair_wrapper_args(tool, {"text": "hi"}) == {"text": "hi"}

    def test_different_tools_keep_distinct_validator_behavior(self, tmp_path):
        first_dir = _create_tool(tmp_path, "first", MINIMAL_TOML.replace('name = "echo"', 'name = "first"'))
        second_dir = _create_tool(tmp_path, "second", MINIMAL_TOML.replace('name = "echo"', 'name = "second"'))
        _write_validator(
            first_dir,
            "def validate_args(args, context):\n"
            "    return ['first validator']\n",
        )
        _write_validator(
            second_dir,
            "def validate_args(args, context):\n"
            "    return ['second validator']\n",
        )
        invalidate_wrappers_cache()
        tools = {tool["name"]: tool for tool in discover_wrappers(tmp_path)}

        assert validate_wrapper_args_semantic(tools["first"], {"text": "hi"}) == ["first validator"]
        assert validate_wrapper_args_semantic(tools["second"], {"text": "hi"}) == ["second validator"]

    def test_aider_validator_rejects_mixed_instruction_like_file_entries(self):
        tool_dir = (
            Path(__file__).resolve().parents[2]
            / "plugins"
            / "tool-aider"
        )
        tool = {
            "name": "aider",
            "path": str(tool_dir),
            "args_schema": {
                "message": {"type": "string", "required": True},
                "files": {"type": "string", "required": False},
                "read_only_files": {"type": "string", "required": False},
            },
            "summary": "aider",
            "usage_guide": "aider",
        }

        errors = validate_wrapper_args_semantic(
            tool,
            {
                "message": "Create the script",
                "files": "text_stats.py, Write a Python script that reads stdin and prints counts.",
            },
            {"phase": "planner"},
        )

        assert any("file paths only" in error for error in errors)

    def test_aider_validator_allows_legitimate_multi_file_paths(self):
        tool_dir = (
            Path(__file__).resolve().parents[2]
            / "plugins"
            / "tool-aider"
        )
        tool = {
            "name": "aider",
            "path": str(tool_dir),
            "args_schema": {
                "message": {"type": "string", "required": True},
                "files": {"type": "string", "required": False},
                "read_only_files": {"type": "string", "required": False},
            },
            "summary": "aider",
            "usage_guide": "aider",
        }

        errors = validate_wrapper_args_semantic(
            tool,
            {
                "message": "Update the implementation",
                "files": "text_stats.py, tests/test_text_stats.py",
                "read_only_files": "README.md, docs/spec.md",
            },
            {"phase": "planner"},
        )

        assert errors == []


# --- auto_correct_wrapper_args ---

class TestAutoCorrectToolArgs:
    """fix common LLM arg name hallucinations."""

    BROWSER_SCHEMA = {
        "action": {"type": "string", "required": True},
        "element": {"type": "string", "required": False},
        "value": {"type": "string", "required": False},
        "url": {"type": "string", "required": False},
    }

    def test_corrects_selector_and_text(self):
        args = {"selector": "[8]", "action": "fill", "text": "hello"}
        result = auto_correct_wrapper_args(args, self.BROWSER_SCHEMA)
        assert result == {"element": "[8]", "action": "fill", "value": "hello"}

    def test_no_overwrite_existing_canonical(self):
        args = {"selector": "[8]", "element": "[5]", "action": "click"}
        result = auto_correct_wrapper_args(args, self.BROWSER_SCHEMA)
        assert result["element"] == "[5]"
        assert "selector" in result  # not removed since canonical exists

    def test_unknown_alias_passthrough(self):
        args = {"action": "click", "foobar": "baz"}
        result = auto_correct_wrapper_args(args, self.BROWSER_SCHEMA)
        assert result == {"action": "click", "foobar": "baz"}

    def test_no_correction_when_canonical_not_in_schema(self):
        schema = {"action": {"type": "string", "required": True}}
        args = {"action": "fill", "text": "hello"}
        result = auto_correct_wrapper_args(args, schema)
        assert result == {"action": "fill", "text": "hello"}  # no change

    def test_query_alias_to_value(self):
        args = {"action": "fill", "query": "test input"}
        result = auto_correct_wrapper_args(args, self.BROWSER_SCHEMA)
        assert result == {"action": "fill", "value": "test input"}


# --- _check_args_depth ---

class TestCheckArgsDepth:
    def test_flat(self):
        assert _check_args_depth({"a": 1, "b": "two"}) is True

    def test_within_limit(self):
        obj = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        assert _check_args_depth(obj) is True

    def test_exceeds_limit(self):
        obj: dict = {}
        current = obj
        for _ in range(MAX_ARGS_DEPTH + 2):
            current["x"] = {}
            current = current["x"]
        assert _check_args_depth(obj) is False

    def test_list_depth(self):
        obj = {"a": [[[[[["deep"]]]]]]}
        assert _check_args_depth(obj) is False

    def test_scalar(self):
        assert _check_args_depth("hello") is True
        assert _check_args_depth(42) is True


# --- _coerce_value ---

class TestCoerceValue:
    def test_string_ok(self):
        assert _coerce_value("hello", "string") == "hello"

    def test_string_wrong(self):
        with pytest.raises(ValueError, match="expected string"):
            _coerce_value(123, "string")

    def test_int_ok(self):
        assert _coerce_value(42, "int") == 42

    def test_int_bool_rejected(self):
        with pytest.raises(ValueError, match="expected int, got bool"):
            _coerce_value(True, "int")

    def test_int_wrong(self):
        with pytest.raises(ValueError, match="expected int"):
            _coerce_value("42", "int")

    def test_float_ok(self):
        assert _coerce_value(3.14, "float") == 3.14

    def test_float_from_int(self):
        assert _coerce_value(5, "float") == 5.0

    def test_float_bool_rejected(self):
        with pytest.raises(ValueError, match="expected float, got bool"):
            _coerce_value(False, "float")

    def test_float_string_rejected(self):
        with pytest.raises(ValueError, match="expected float"):
            _coerce_value("3.14", "float")

    def test_bool_ok(self):
        assert _coerce_value(True, "bool") is True

    def test_bool_wrong(self):
        with pytest.raises(ValueError, match="expected bool"):
            _coerce_value(1, "bool")

    def test_unknown_type(self):
        with pytest.raises(ValueError, match="unknown type"):
            _coerce_value("x", "date")


# --- build_wrapper_input ---

class TestBuildToolInput:
    def _make_tool(self, session_secrets=None):
        return {
            "name": "echo",
            "summary": "Echo",
            "args_schema": {},
            "env": {},
            "session_secrets": session_secrets or [],
            "path": "/fake",
            "version": "0.1.0",
            "description": "",
        }

    def test_basic_input(self):
        tool = self._make_tool()
        result = build_wrapper_input(tool, {"text": "hi"}, "sess1", "/workspace")
        assert result["args"] == {"text": "hi"}
        assert result["session"] == "sess1"
        assert result["workspace"] == "/workspace"
        assert result["session_secrets"] == {}
        assert result["plan_outputs"] == []

    def test_with_plan_outputs(self):
        tool = self._make_tool()
        outputs = [{"index": 1, "type": "exec", "detail": "ls", "output": "a\nb", "status": "done"}]
        result = build_wrapper_input(tool, {}, "sess1", "/ws", plan_outputs=outputs)
        assert result["plan_outputs"] == outputs

    def test_scoped_session_secrets(self):
        tool = self._make_tool(session_secrets=["api_token"])
        secrets = {"api_token": "tok_123", "other_secret": "should_not_appear"}
        result = build_wrapper_input(tool, {}, "sess1", "/ws", session_secrets=secrets)
        assert result["session_secrets"] == {"api_token": "tok_123"}
        assert "other_secret" not in result["session_secrets"]

    def test_no_declared_secrets_scoped_empty(self):
        tool = self._make_tool(session_secrets=[])
        secrets = {"api_token": "tok_123"}
        result = build_wrapper_input(tool, {}, "sess1", "/ws", session_secrets=secrets)
        assert result["session_secrets"] == {}

    def test_none_session_secrets(self):
        tool = self._make_tool(session_secrets=["api_token"])
        result = build_wrapper_input(tool, {}, "sess1", "/ws", session_secrets=None)
        assert result["session_secrets"] == {}


# --- build_wrapper_env ---

class TestBuildToolEnv:
    def test_basic_env(self):
        tool = {"name": "echo", "env": {}}
        with patch.dict(os.environ, {}, clear=True):
            os.environ["PATH"] = "/usr/bin"
            env = build_wrapper_env(tool)
        assert "PATH" in env
        assert len(env) == 1

    def test_env_var_present(self):
        tool = {"name": "search", "env": {"api_key": {"required": True}}}
        with patch.dict(os.environ, {"KISO_WRAPPER_SEARCH_API_KEY": "sk-123"}):
            env = build_wrapper_env(tool)
        assert env["KISO_WRAPPER_SEARCH_API_KEY"] == "sk-123"

    def test_env_var_missing_required(self):
        tool = {"name": "search", "env": {"api_key": {"required": True}}}
        # Remove the var if set
        with patch.dict(os.environ, {}, clear=True):
            os.environ["PATH"] = "/usr/bin"
            env = build_wrapper_env(tool)
        # Should not include missing var, just PATH
        assert "KISO_WRAPPER_SEARCH_API_KEY" not in env

    def test_env_var_missing_optional(self):
        tool = {"name": "search", "env": {"api_key": {"required": False}}}
        with patch.dict(os.environ, {}, clear=True):
            os.environ["PATH"] = "/usr/bin"
            env = build_wrapper_env(tool)
        assert "KISO_WRAPPER_SEARCH_API_KEY" not in env

    def test_venv_bin_in_path(self, tmp_path):
        """Tool's .venv/bin/ is prepended to PATH so pip-installed CLIs are found."""
        tool = {"name": "browser", "env": {}, "path": str(tmp_path / "wrappers" / "browser")}
        env = build_wrapper_env(tool)
        assert env["PATH"].startswith(str(tmp_path / "wrappers" / "browser" / ".venv" / "bin"))

    def test_multiple_env_vars(self):
        tool = {
            "name": "search",
            "env": {
                "api_key": {"required": True},
                "token": {"required": False},
            },
        }
        with patch.dict(os.environ, {
            "KISO_WRAPPER_SEARCH_API_KEY": "key1",
            "KISO_WRAPPER_SEARCH_TOKEN": "tok1",
        }):
            env = build_wrapper_env(tool)
        assert env["KISO_WRAPPER_SEARCH_API_KEY"] == "key1"
        assert env["KISO_WRAPPER_SEARCH_TOKEN"] == "tok1"


    def test_no_fallback_when_neither_set(self):
        """Var missing when not set — warning logged."""
        tool = {"name": "aider", "env": {"api_key": {"required": True}}}
        with patch.dict(os.environ, {}, clear=True):
            os.environ["PATH"] = "/usr/bin"
            env = build_wrapper_env(tool)
        assert "KISO_WRAPPER_AIDER_API_KEY" not in env

    def test_llm_api_key_propagated(self):
        """Base LLM key is included in tool env when set."""
        tool = {"name": "echo", "env": {}}
        with patch.dict(os.environ, {"KISO_LLM_API_KEY": "sk-base-key"}):
            env = build_wrapper_env(tool)
        assert env["KISO_LLM_API_KEY"] == "sk-base-key"

    def test_llm_api_key_not_included_when_unset(self):
        """Base LLM key is omitted when not set — no empty string leak."""
        tool = {"name": "echo", "env": {}}
        with patch.dict(os.environ, {}, clear=True):
            os.environ["PATH"] = "/usr/bin"
            env = build_wrapper_env(tool)
        assert "KISO_LLM_API_KEY" not in env


# --- discover_wrappers TTL cache behaviour ---


class TestDiscoverToolsCache:
    def test_cached_within_ttl(self, tmp_path):
        """discover_wrappers() returns cached result within TTL window."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)

        with patch("kiso.wrappers.KISO_DIR", tmp_path):
            invalidate_wrappers_cache()
            result1 = discover_wrappers()
            assert len(result1) == 1

            # Install a second tool — without invalidation, still see cached result
            _create_tool(tools_dir, "newtool", MINIMAL_TOML.replace(
                'name = "echo"', 'name = "newtool"'
            ))
            result2 = discover_wrappers()
            assert len(result2) == 1  # still cached

            # After invalidation, new tool is visible
            invalidate_wrappers_cache()
            result3 = discover_wrappers()
            assert len(result3) == 2

    def test_invalidate_clears_cache(self, tmp_path):
        """invalidate_wrappers_cache() causes the next call to rescan."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", MINIMAL_TOML)

        with patch("kiso.wrappers.KISO_DIR", tmp_path):
            invalidate_wrappers_cache()
            discover_wrappers()  # populate cache

            _create_tool(tools_dir, "newtool", MINIMAL_TOML.replace(
                'name = "echo"', 'name = "newtool"'
            ))

            invalidate_wrappers_cache()
            result = discover_wrappers()

        assert len(result) == 2


# ---------------------------------------------------------------------------
# — consumes field + file processing routing section
# ---------------------------------------------------------------------------


class TestM826ConsumesField:
    """consumes field in kiso.toml → file routing in planner tool list."""

    def _make_tool(self, name, summary, consumes=None):
        return {
            "name": name,
            "summary": summary,
            "args_schema": {},
            "env": {},
            "session_secrets": [],
            "path": "/fake",
            "version": "0.1.0",
            "description": "",
            "consumes": consumes or [],
        }

    def test_tools_with_consumes_generate_routing(self):
        """Tools with consumes → 'File processing' section in tool list."""
        tools = [
            self._make_tool("ocr", "Image OCR — extract text", consumes=["image"]),
            self._make_tool("docreader", "Read documents", consumes=["document"]),
            self._make_tool("transcriber", "Transcribe audio", consumes=["audio"]),
        ]
        result = build_planner_wrapper_list(tools, "admin")
        assert "File processing" in result
        assert "image files → ocr (Image OCR)" in result
        assert "document files → docreader (Read documents)" in result
        assert "audio files → transcriber (Transcribe audio)" in result

    def test_no_consumes_no_section(self):
        """Tools without consumes → no 'File processing' section."""
        tools = [
            self._make_tool("browser", "Navigate pages"),
            self._make_tool("websearch", "Search the web"),
        ]
        result = build_planner_wrapper_list(tools, "admin")
        assert "File processing" not in result

    def test_mixed_tools(self):
        """Mix of tools with/without consumes → only declared types shown."""
        tools = [
            self._make_tool("ocr", "Image OCR — extract text", consumes=["image"]),
            self._make_tool("browser", "Navigate pages"),
        ]
        result = build_planner_wrapper_list(tools, "admin")
        assert "File processing" in result
        assert "image files → ocr" in result
        assert "web_page" not in result  # browser has no consumes

    def test_multiple_tools_same_type(self):
        """Two tools consuming image → both listed on same line."""
        tools = [
            self._make_tool("ocr", "Extract text from images", consumes=["image"]),
            self._make_tool("describe", "Describe image contents", consumes=["image"]),
        ]
        result = build_planner_wrapper_list(tools, "admin")
        assert "image files →" in result
        assert "ocr" in result
        assert "describe" in result

    def test_discover_wrappers_parses_consumes(self, tmp_path):
        """discover_wrappers() includes consumes in tool info dict."""
        tool_dir = tmp_path / "wrappers" / "ocr"
        tool_dir.mkdir(parents=True)
        (tool_dir / "run.py").write_text("pass")
        (tool_dir / "pyproject.toml").write_text('[project]\nname = "ocr"\nversion = "0.1.0"')
        (tool_dir / "kiso.toml").write_text("""
[kiso]
type = "tool"
name = "ocr"
version = "0.1.0"
description = "OCR tool"

[kiso.tool]
summary = "Extract text from images"
usage_guide = "Use this to extract text from images."
consumes = ["image"]

[kiso.tool.args]
file_path = { type = "string", required = true, description = "path to image" }
""")
        invalidate_wrappers_cache()
        result = discover_wrappers(tmp_path / "wrappers")
        assert len(result) == 1
        assert result[0]["consumes"] == ["image"]

    def test_unknown_consumes_skipped(self, tmp_path):
        """Unknown consumes value → warned and skipped."""
        tool_dir = tmp_path / "wrappers" / "exotic"
        tool_dir.mkdir(parents=True)
        (tool_dir / "run.py").write_text("pass")
        (tool_dir / "pyproject.toml").write_text('[project]\nname = "exotic"\nversion = "0.1.0"')
        (tool_dir / "kiso.toml").write_text("""
[kiso]
type = "tool"
name = "exotic"
version = "0.1.0"
description = "Exotic tool"

[kiso.tool]
summary = "Process exotic formats"
usage_guide = "Handles exotic data."
consumes = ["hologram", "image"]

[kiso.tool.args]
""")
        invalidate_wrappers_cache()
        result = discover_wrappers(tmp_path / "wrappers")
        assert len(result) == 1
        # "hologram" skipped, "image" kept
        assert result[0]["consumes"] == ["image"]
