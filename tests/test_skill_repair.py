"""Tests for kiso.skill_repair — auto-repair unhealthy skills on startup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.skill_repair import repair_unhealthy_skills


# Minimal valid kiso.toml for a skill with a binary dep
_TOML_WITH_DEP = """\
[kiso]
type = "skill"
name = "{name}"
version = "0.1.0"

[kiso.skill]
summary = "Test skill"
usage_guide = "test"

[kiso.skill.args]
action = {{ type = "string", required = true }}

[kiso.deps]
bin = ["{binary}"]
"""

_TOML_NO_DEPS = """\
[kiso]
type = "skill"
name = "{name}"
version = "0.1.0"

[kiso.skill]
summary = "Test skill"
usage_guide = "test"

[kiso.skill.args]
action = { type = "string", required = true }
"""


def _create_skill(skills_dir: Path, name: str, binary: str = "nonexistent_xyz",
                   deps_sh: str | None = None, has_deps: bool = True) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    if has_deps:
        toml = _TOML_WITH_DEP.format(name=name, binary=binary)
    else:
        toml = _TOML_NO_DEPS.format(name=name)
    (skill_dir / "kiso.toml").write_text(toml)
    (skill_dir / "run.py").write_text("pass")
    (skill_dir / "pyproject.toml").write_text(f'[project]\nname="{name}"\nversion="0.1.0"')
    if deps_sh is not None:
        (skill_dir / "deps.sh").write_text(deps_sh)
        (skill_dir / "deps.sh").chmod(0o755)
    return skill_dir


class TestRepairUnhealthySkills:
    async def test_no_unhealthy_skills_returns_empty(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "echo", binary="bash", has_deps=True)
        result = await repair_unhealthy_skills(skills_dir)
        assert result == []

    async def test_no_skills_at_all(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        result = await repair_unhealthy_skills(skills_dir)
        assert result == []

    async def test_unhealthy_skill_runs_deps_sh(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        marker = tmp_path / "repaired.marker"
        _create_skill(
            skills_dir, "browser",
            binary="nonexistent_xyz_12345",
            deps_sh=f"#!/bin/bash\ntouch {marker}",
        )
        result = await repair_unhealthy_skills(skills_dir)
        assert "browser" in result
        assert marker.exists(), "deps.sh should have been executed"

    async def test_unhealthy_without_deps_sh_skipped(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "broken", binary="nonexistent_xyz_12345", deps_sh=None)
        result = await repair_unhealthy_skills(skills_dir)
        assert result == []

    async def test_deps_sh_failure_does_not_crash(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(
            skills_dir, "bad",
            binary="nonexistent_xyz_12345",
            deps_sh="#!/bin/bash\nexit 1",
        )
        result = await repair_unhealthy_skills(skills_dir)
        assert "bad" in result  # attempted, didn't crash

    async def test_multiple_unhealthy_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        m1 = tmp_path / "m1.marker"
        m2 = tmp_path / "m2.marker"
        _create_skill(skills_dir, "skill-a", binary="nonexistent_a",
                       deps_sh=f"#!/bin/bash\ntouch {m1}")
        _create_skill(skills_dir, "skill-b", binary="nonexistent_b",
                       deps_sh=f"#!/bin/bash\ntouch {m2}")
        result = await repair_unhealthy_skills(skills_dir)
        assert len(result) == 2
        assert m1.exists()
        assert m2.exists()

    async def test_healthy_skill_not_repaired(self, tmp_path):
        """A healthy skill (bash exists) should not have deps.sh re-run."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        marker = tmp_path / "should_not_exist.marker"
        _create_skill(skills_dir, "healthy", binary="bash",
                       deps_sh=f"#!/bin/bash\ntouch {marker}")
        result = await repair_unhealthy_skills(skills_dir)
        assert result == []
        assert not marker.exists()

    async def test_cache_invalidated_after_repair(self, tmp_path):
        """Skills cache should be invalidated after repairs."""
        from kiso.skills import _skills_cache
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "fix-me", binary="nonexistent_xyz",
                       deps_sh="#!/bin/bash\ntrue")
        # Pre-populate cache
        from kiso.skills import discover_skills
        discover_skills(skills_dir)
        assert skills_dir in _skills_cache

        await repair_unhealthy_skills(skills_dir)
        assert skills_dir not in _skills_cache
