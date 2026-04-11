"""Tests for kiso.wrapper_repair — auto-repair unhealthy tools on startup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.wrapper_repair import (
    repair_unhealthy_wrappers, rerun_all_deps,
    _is_container_rebuilt, _mark_image_id,
)


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
action = {{ type = "string", required = true }}
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
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "echo", binary="bash", has_deps=True)
        result = await repair_unhealthy_wrappers(tools_dir)
        assert result == []

    async def test_no_tools_at_all(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        result = await repair_unhealthy_wrappers(tools_dir)
        assert result == []

    async def test_unhealthy_tool_runs_deps_sh(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        marker = tmp_path / "repaired.marker"
        _create_tool(
            tools_dir, "browser",
            binary="nonexistent_xyz_12345",
            deps_sh=f"#!/bin/bash\ntouch {marker}",
        )
        result = await repair_unhealthy_wrappers(tools_dir)
        assert "browser" in result
        assert marker.exists(), "deps.sh should have been executed"

    async def test_unhealthy_without_deps_sh_skipped(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "broken", binary="nonexistent_xyz_12345", deps_sh=None)
        result = await repair_unhealthy_wrappers(tools_dir)
        assert result == []

    async def test_deps_sh_failure_does_not_crash(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(
            tools_dir, "bad",
            binary="nonexistent_xyz_12345",
            deps_sh="#!/bin/bash\nexit 1",
        )
        result = await repair_unhealthy_wrappers(tools_dir)
        assert "bad" in result  # attempted, didn't crash

    async def test_multiple_unhealthy_tools(self, tmp_path):
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        m1 = tmp_path / "m1.marker"
        m2 = tmp_path / "m2.marker"
        _create_tool(tools_dir, "tool-a", binary="nonexistent_a",
                      deps_sh=f"#!/bin/bash\ntouch {m1}")
        _create_tool(tools_dir, "tool-b", binary="nonexistent_b",
                      deps_sh=f"#!/bin/bash\ntouch {m2}")
        result = await repair_unhealthy_wrappers(tools_dir)
        assert len(result) == 2
        assert m1.exists()
        assert m2.exists()

    async def test_healthy_tool_not_repaired(self, tmp_path):
        """A healthy tool (bash exists) should not have deps.sh re-run."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        marker = tmp_path / "should_not_exist.marker"
        _create_tool(tools_dir, "healthy", binary="bash",
                      deps_sh=f"#!/bin/bash\ntouch {marker}")
        result = await repair_unhealthy_wrappers(tools_dir)
        assert result == []
        assert not marker.exists()

    async def test_cache_invalidated_after_repair(self, tmp_path):
        """Tools cache should be invalidated after repairs."""
        from kiso.wrappers import _wrappers_cache
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "fix-me", binary="nonexistent_xyz",
                      deps_sh="#!/bin/bash\ntrue")
        # Pre-populate cache
        from kiso.wrappers import discover_wrappers
        discover_wrappers(tools_dir)
        assert tools_dir in _wrappers_cache

        await repair_unhealthy_wrappers(tools_dir)
        assert tools_dir not in _wrappers_cache


# ---------------------------------------------------------------------------
# Container rebuild detection
# ---------------------------------------------------------------------------


class TestContainerRebuildDetection:
    def test_no_image_id_file(self, tmp_path):
        """No .image_id file → not rebuilt (not Docker or old image)."""
        with patch("kiso.wrapper_repair._IMAGE_ID_PATH", tmp_path / "nope"):
            assert _is_container_rebuilt() is False

    def test_first_boot(self, tmp_path):
        """Image ID exists but no last_image_id → first boot → rebuilt."""
        image_id = tmp_path / ".image_id"
        image_id.write_text("abc123")
        with (
            patch("kiso.wrapper_repair._IMAGE_ID_PATH", image_id),
            patch("kiso.wrapper_repair._LAST_IMAGE_ID_PATH", tmp_path / ".last_image_id"),
        ):
            assert _is_container_rebuilt() is True

    def test_same_image(self, tmp_path):
        """Same image ID → not rebuilt."""
        image_id = tmp_path / ".image_id"
        image_id.write_text("abc123")
        last = tmp_path / ".last_image_id"
        last.write_text("abc123")
        with (
            patch("kiso.wrapper_repair._IMAGE_ID_PATH", image_id),
            patch("kiso.wrapper_repair._LAST_IMAGE_ID_PATH", last),
        ):
            assert _is_container_rebuilt() is False

    def test_different_image(self, tmp_path):
        """Different image ID → rebuilt."""
        image_id = tmp_path / ".image_id"
        image_id.write_text("new456")
        last = tmp_path / ".last_image_id"
        last.write_text("old123")
        with (
            patch("kiso.wrapper_repair._IMAGE_ID_PATH", image_id),
            patch("kiso.wrapper_repair._LAST_IMAGE_ID_PATH", last),
        ):
            assert _is_container_rebuilt() is True

    def test_mark_image_id(self, tmp_path):
        """mark_image_id persists current image ID."""
        image_id = tmp_path / ".image_id"
        image_id.write_text("xyz789")
        last = tmp_path / ".last_image_id"
        with (
            patch("kiso.wrapper_repair._IMAGE_ID_PATH", image_id),
            patch("kiso.wrapper_repair._LAST_IMAGE_ID_PATH", last),
        ):
            _mark_image_id()
        assert last.read_text() == "xyz789"


# ---------------------------------------------------------------------------
# rerun_all_deps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRerunAllDeps:
    async def test_reruns_deps_for_all_tools(self, tmp_path):
        """Re-runs deps.sh for ALL tools, not just unhealthy ones."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        m1 = tmp_path / "m1.marker"
        m2 = tmp_path / "m2.marker"
        # Both tools are healthy (bash exists) but deps.sh still runs
        _create_tool(tools_dir, "tool-a", binary="bash",
                      deps_sh=f"#!/bin/bash\ntouch {m1}")
        _create_tool(tools_dir, "tool-b", binary="bash",
                      deps_sh=f"#!/bin/bash\ntouch {m2}")
        result = await rerun_all_deps(tools_dir)
        assert len(result) == 2
        assert m1.exists()
        assert m2.exists()

    async def test_no_tools(self, tmp_path):
        """No installed tools → empty list, no errors."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        result = await rerun_all_deps(tools_dir)
        assert result == []

    async def test_skips_tools_without_deps_sh(self, tmp_path):
        """Tools without deps.sh are skipped."""
        tools_dir = tmp_path / "wrappers"
        tools_dir.mkdir()
        _create_tool(tools_dir, "no-deps", binary="bash", has_deps=False)
        # No deps.sh created
        assert not (tools_dir / "no-deps" / "deps.sh").exists()
        result = await rerun_all_deps(tools_dir)
        assert result == []
