"""End-to-end smoke test for tool lifecycle — broken → repair → healthy.

Simulates the real failure scenario: tool dir persists on volume after image
rebuild, system deps are gone, tool appears installed but broken, auto-repair
fixes it.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.wrappers import (
    build_planner_wrapper_list,
    discover_wrappers,
    invalidate_wrappers_cache,
)
from kiso.wrapper_repair import repair_unhealthy_wrappers


# Minimal valid kiso.toml
_TOML = """\
[kiso]
type = "tool"
name = "{name}"
version = "0.1.0"

[kiso.tool]
summary = "{summary}"
usage_guide = "test guide"

[kiso.tool.args]
action = {{ type = "string", required = true }}

[kiso.deps]
bin = ["{binary}"]
"""


def _create_tool(tools_dir: Path, name: str, summary: str,
                  binary: str, deps_sh: str | None = None) -> Path:
    tool_dir = tools_dir / name
    tool_dir.mkdir(parents=True)
    toml = _TOML.format(name=name, summary=summary, binary=binary)
    (tool_dir / "kiso.toml").write_text(toml)
    (tool_dir / "run.py").write_text("pass")
    (tool_dir / "pyproject.toml").write_text(
        f'[project]\nname="{name}"\nversion="0.1.0"'
    )
    if deps_sh is not None:
        (tool_dir / "deps.sh").write_text(deps_sh)
        (tool_dir / "deps.sh").chmod(0o755)
    return tool_dir


class TestToolLifecycleRecovery:
    """Full cycle: broken tool → detected → planner warned → repaired → healthy."""

    def test_broken_tool_detected_and_annotated(self, tmp_path):
        """Step 1-2: discover_wrappers finds tool with healthy=False,
        build_planner_wrapper_list shows [BROKEN] annotation."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "browser", "Browser automation",
                     binary="nonexistent_playwright_xyz")

        invalidate_wrappers_cache()
        tools = discover_wrappers(tools_dir)
        assert len(tools) == 1
        assert tools[0]["name"] == "browser"
        assert tools[0]["healthy"] is False
        assert "nonexistent_playwright_xyz" in tools[0]["missing_deps"]

        # Planner sees the broken annotation
        tool_list = build_planner_wrapper_list(tools, "admin")
        assert "[BROKEN" in tool_list
        assert "missing: nonexistent_playwright_xyz" in tool_list
        assert "kiso tool remove browser" in tool_list

    async def test_repair_fixes_broken_tool(self, tmp_path):
        """Step 3: repair_unhealthy_wrappers runs deps.sh which installs
        the missing binary, then re-discovery shows healthy=True."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()

        # Create a fake binary dir that deps.sh will populate
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # The "binary" we check for. Not on PATH initially.
        fake_binary = bin_dir / "fake_playwright"

        # deps.sh creates the binary
        deps_script = f"#!/bin/bash\ntouch {fake_binary} && chmod +x {fake_binary}"

        _create_tool(tools_dir, "browser", "Browser automation",
                     binary="fake_playwright", deps_sh=deps_script)

        # Phase 1: tool is broken
        invalidate_wrappers_cache()
        tools = discover_wrappers(tools_dir)
        assert tools[0]["healthy"] is False

        # Phase 2: repair runs deps.sh
        repaired = await repair_unhealthy_wrappers(tools_dir)
        assert "browser" in repaired
        assert fake_binary.exists()

        # Phase 3: re-discover with updated PATH
        invalidate_wrappers_cache()
        env_path = os.environ.get("PATH", "") + ":" + str(bin_dir)
        with patch.dict(os.environ, {"PATH": env_path}):
            tools = discover_wrappers(tools_dir)

        assert len(tools) == 1
        assert tools[0]["healthy"] is True
        assert tools[0]["missing_deps"] == []

        # Planner no longer sees [BROKEN]
        tool_list = build_planner_wrapper_list(tools, "admin")
        assert "[BROKEN" not in tool_list
        assert "- browser — Browser automation" in tool_list

    def test_healthy_tool_stays_healthy(self, tmp_path):
        """Healthy tool is not touched by the repair flow."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        marker = tmp_path / "should_not_run.marker"
        _create_tool(tools_dir, "echo", "Echo skill",
                     binary="bash",
                     deps_sh=f"#!/bin/bash\ntouch {marker}")

        invalidate_wrappers_cache()
        tools = discover_wrappers(tools_dir)
        assert tools[0]["healthy"] is True

        tool_list = build_planner_wrapper_list(tools, "admin")
        assert "- echo — Echo skill" in tool_list
        assert "[BROKEN" not in tool_list
        assert not marker.exists()  # deps.sh was never called

    def test_mixed_healthy_and_broken(self, tmp_path):
        """Multiple tools: one healthy, one broken — only broken is flagged."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", "Echo skill", binary="bash")
        _create_tool(tools_dir, "browser", "Browser automation",
                     binary="nonexistent_xyz_12345")

        invalidate_wrappers_cache()
        tools = discover_wrappers(tools_dir)
        assert len(tools) == 2

        healthy = [s for s in tools if s["healthy"]]
        broken = [s for s in tools if not s["healthy"]]
        assert len(healthy) == 1
        assert len(broken) == 1
        assert healthy[0]["name"] == "echo"
        assert broken[0]["name"] == "browser"

        tool_list = build_planner_wrapper_list(tools, "admin")
        # echo is clean
        assert "- echo — Echo skill" in tool_list
        # browser is annotated
        assert "[BROKEN" in tool_list
        assert "nonexistent_xyz_12345" in tool_list

    async def test_full_cycle_end_to_end(self, tmp_path):
        """Complete lifecycle: install → image rebuild (deps gone) → detect →
        repair → recover. Simulates the exact real-world failure."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_bin = bin_dir / "my_tool"

        # Simulate: tool was installed, binary existed, then image was rebuilt
        # (binary is gone). Tool dir persists on volume.
        _create_tool(
            tools_dir, "my-skill", "My tool",
            binary="my_tool",
            deps_sh=f"#!/bin/bash\ntouch {fake_bin} && chmod +x {fake_bin}",
        )

        # 1. Discovery: broken
        invalidate_wrappers_cache()
        tools = discover_wrappers(tools_dir)
        assert tools[0]["healthy"] is False

        # 2. Planner sees broken annotation
        tool_list = build_planner_wrapper_list(tools, "admin")
        assert "[BROKEN" in tool_list

        # 3. Auto-repair on startup
        repaired = await repair_unhealthy_wrappers(tools_dir)
        assert "my-skill" in repaired
        assert fake_bin.exists()

        # 4. Re-discovery: healthy
        invalidate_wrappers_cache()
        env_path = os.environ.get("PATH", "") + ":" + str(bin_dir)
        with patch.dict(os.environ, {"PATH": env_path}):
            tools = discover_wrappers(tools_dir)

        assert tools[0]["healthy"] is True

        # 5. Planner sees clean tool
        tool_list = build_planner_wrapper_list(tools, "admin")
        assert "[BROKEN" not in tool_list
        assert "- my-skill — My tool" in tool_list
