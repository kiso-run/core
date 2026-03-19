"""Tests for kiso.tool_repair — auto-repair unhealthy tools on startup."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.tool_repair import repair_unhealthy_tools


# Minimal valid kiso.toml for a tool with a binary dep
_TOML_WITH_DEP = """\
[kiso]
type = "tool"
name = "{name}"
version = "0.1.0"

[kiso.tool]
summary = "Test tool"
usage_guide = "test"

[kiso.tool.args]
action = {{ type = "string", required = true }}

[kiso.deps]
bin = ["{binary}"]
"""

_TOML_NO_DEPS = """\
[kiso]
type = "tool"
name = "{name}"
version = "0.1.0"

[kiso.tool]
summary = "Test tool"
usage_guide = "test"

[kiso.tool.args]
action = { type = "string", required = true }
"""


def _create_tool(tools_dir: Path, name: str, binary: str = "nonexistent_xyz",
                  deps_sh: str | None = None, has_deps: bool = True) -> Path:
    tool_dir = tools_dir / name
    tool_dir.mkdir(parents=True)
    if has_deps:
        toml = _TOML_WITH_DEP.format(name=name, binary=binary)
    else:
        toml = _TOML_NO_DEPS.format(name=name)
    (tool_dir / "kiso.toml").write_text(toml)
    (tool_dir / "run.py").write_text("pass")
    (tool_dir / "pyproject.toml").write_text(f'[project]\nname="{name}"\nversion="0.1.0"')
    if deps_sh is not None:
        (tool_dir / "deps.sh").write_text(deps_sh)
        (tool_dir / "deps.sh").chmod(0o755)
    return tool_dir


class TestRepairUnhealthyTools:
    async def test_no_unhealthy_tools_returns_empty(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", binary="bash", has_deps=True)
        result = await repair_unhealthy_tools(tools_dir)
        assert result == []

    async def test_no_tools_at_all(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        result = await repair_unhealthy_tools(tools_dir)
        assert result == []

    async def test_unhealthy_tool_runs_deps_sh(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        marker = tmp_path / "repaired.marker"
        _create_tool(
            tools_dir, "browser",
            binary="nonexistent_xyz_12345",
            deps_sh=f"#!/bin/bash\ntouch {marker}",
        )
        result = await repair_unhealthy_tools(tools_dir)
        assert "browser" in result
        assert marker.exists(), "deps.sh should have been executed"

    async def test_unhealthy_without_deps_sh_skipped(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _create_tool(tools_dir, "broken", binary="nonexistent_xyz_12345", deps_sh=None)
        result = await repair_unhealthy_tools(tools_dir)
        assert result == []

    async def test_deps_sh_failure_does_not_crash(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _create_tool(
            tools_dir, "bad",
            binary="nonexistent_xyz_12345",
            deps_sh="#!/bin/bash\nexit 1",
        )
        result = await repair_unhealthy_tools(tools_dir)
        assert "bad" in result  # attempted, didn't crash

    async def test_multiple_unhealthy_tools(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        m1 = tmp_path / "m1.marker"
        m2 = tmp_path / "m2.marker"
        _create_tool(tools_dir, "tool-a", binary="nonexistent_a",
                      deps_sh=f"#!/bin/bash\ntouch {m1}")
        _create_tool(tools_dir, "tool-b", binary="nonexistent_b",
                      deps_sh=f"#!/bin/bash\ntouch {m2}")
        result = await repair_unhealthy_tools(tools_dir)
        assert len(result) == 2
        assert m1.exists()
        assert m2.exists()

    async def test_healthy_tool_not_repaired(self, tmp_path):
        """A healthy tool (bash exists) should not have deps.sh re-run."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        marker = tmp_path / "should_not_exist.marker"
        _create_tool(tools_dir, "healthy", binary="bash",
                      deps_sh=f"#!/bin/bash\ntouch {marker}")
        result = await repair_unhealthy_tools(tools_dir)
        assert result == []
        assert not marker.exists()

    async def test_cache_invalidated_after_repair(self, tmp_path):
        """Tools cache should be invalidated after repairs."""
        from kiso.tools import _tools_cache
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _create_tool(tools_dir, "fix-me", binary="nonexistent_xyz",
                      deps_sh="#!/bin/bash\ntrue")
        # Pre-populate cache
        from kiso.tools import discover_tools
        discover_tools(tools_dir)
        assert tools_dir in _tools_cache

        await repair_unhealthy_tools(tools_dir)
        assert tools_dir not in _tools_cache
