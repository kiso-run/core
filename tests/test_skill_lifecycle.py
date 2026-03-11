"""M182: End-to-end smoke test for skill lifecycle — broken → repair → healthy.

Simulates the real failure scenario: skill dir persists on volume after image
rebuild, system deps are gone, skill appears installed but broken, auto-repair
fixes it.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.skills import (
    build_planner_skill_list,
    discover_skills,
    invalidate_skills_cache,
)
from kiso.skill_repair import repair_unhealthy_skills


# Minimal valid kiso.toml
_TOML = """\
[kiso]
type = "skill"
name = "{name}"
version = "0.1.0"

[kiso.skill]
summary = "{summary}"
usage_guide = "test guide"

[kiso.skill.args]
action = {{ type = "string", required = true }}

[kiso.deps]
bin = ["{binary}"]
"""


def _create_skill(skills_dir: Path, name: str, summary: str,
                   binary: str, deps_sh: str | None = None) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    toml = _TOML.format(name=name, summary=summary, binary=binary)
    (skill_dir / "kiso.toml").write_text(toml)
    (skill_dir / "run.py").write_text("pass")
    (skill_dir / "pyproject.toml").write_text(
        f'[project]\nname="{name}"\nversion="0.1.0"'
    )
    if deps_sh is not None:
        (skill_dir / "deps.sh").write_text(deps_sh)
        (skill_dir / "deps.sh").chmod(0o755)
    return skill_dir


class TestSkillLifecycleRecovery:
    """Full cycle: broken skill → detected → planner warned → repaired → healthy."""

    def test_broken_skill_detected_and_annotated(self, tmp_path):
        """Step 1-2: discover_skills finds skill with healthy=False,
        build_planner_skill_list shows [BROKEN] annotation."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "browser", "Browser automation",
                       binary="nonexistent_playwright_xyz")

        invalidate_skills_cache()
        skills = discover_skills(skills_dir)
        assert len(skills) == 1
        assert skills[0]["name"] == "browser"
        assert skills[0]["healthy"] is False
        assert "nonexistent_playwright_xyz" in skills[0]["missing_deps"]

        # Planner sees the broken annotation
        skill_list = build_planner_skill_list(skills, "admin")
        assert "[BROKEN" in skill_list
        assert "missing: nonexistent_playwright_xyz" in skill_list
        assert "kiso tool remove browser" in skill_list

    async def test_repair_fixes_broken_skill(self, tmp_path):
        """Step 3: repair_unhealthy_skills runs deps.sh which installs
        the missing binary, then re-discovery shows healthy=True."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create a fake binary dir that deps.sh will populate
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # The "binary" we check for. Not on PATH initially.
        fake_binary = bin_dir / "fake_playwright"

        # deps.sh creates the binary
        deps_script = f"#!/bin/bash\ntouch {fake_binary} && chmod +x {fake_binary}"

        _create_skill(skills_dir, "browser", "Browser automation",
                       binary="fake_playwright", deps_sh=deps_script)

        # Phase 1: skill is broken
        invalidate_skills_cache()
        skills = discover_skills(skills_dir)
        assert skills[0]["healthy"] is False

        # Phase 2: repair runs deps.sh
        repaired = await repair_unhealthy_skills(skills_dir)
        assert "browser" in repaired
        assert fake_binary.exists()

        # Phase 3: re-discover with updated PATH
        invalidate_skills_cache()
        env_path = os.environ.get("PATH", "") + ":" + str(bin_dir)
        with patch.dict(os.environ, {"PATH": env_path}):
            skills = discover_skills(skills_dir)

        assert len(skills) == 1
        assert skills[0]["healthy"] is True
        assert skills[0]["missing_deps"] == []

        # Planner no longer sees [BROKEN]
        skill_list = build_planner_skill_list(skills, "admin")
        assert "[BROKEN" not in skill_list
        assert "- browser — Browser automation" in skill_list

    def test_healthy_skill_stays_healthy(self, tmp_path):
        """Healthy skill is not touched by the repair flow."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        marker = tmp_path / "should_not_run.marker"
        _create_skill(skills_dir, "echo", "Echo skill",
                       binary="bash",
                       deps_sh=f"#!/bin/bash\ntouch {marker}")

        invalidate_skills_cache()
        skills = discover_skills(skills_dir)
        assert skills[0]["healthy"] is True

        skill_list = build_planner_skill_list(skills, "admin")
        assert "- echo — Echo skill" in skill_list
        assert "[BROKEN" not in skill_list
        assert not marker.exists()  # deps.sh was never called

    def test_mixed_healthy_and_broken(self, tmp_path):
        """Multiple skills: one healthy, one broken — only broken is flagged."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "echo", "Echo skill", binary="bash")
        _create_skill(skills_dir, "browser", "Browser automation",
                       binary="nonexistent_xyz_12345")

        invalidate_skills_cache()
        skills = discover_skills(skills_dir)
        assert len(skills) == 2

        healthy = [s for s in skills if s["healthy"]]
        broken = [s for s in skills if not s["healthy"]]
        assert len(healthy) == 1
        assert len(broken) == 1
        assert healthy[0]["name"] == "echo"
        assert broken[0]["name"] == "browser"

        skill_list = build_planner_skill_list(skills, "admin")
        # echo is clean
        assert "- echo — Echo skill" in skill_list
        # browser is annotated
        assert "[BROKEN" in skill_list
        assert "nonexistent_xyz_12345" in skill_list

    async def test_full_cycle_end_to_end(self, tmp_path):
        """Complete lifecycle: install → image rebuild (deps gone) → detect →
        repair → recover. Simulates the exact real-world failure."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_bin = bin_dir / "my_tool"

        # Simulate: skill was installed, binary existed, then image was rebuilt
        # (binary is gone). Skill dir persists on volume.
        _create_skill(
            skills_dir, "my-skill", "My tool",
            binary="my_tool",
            deps_sh=f"#!/bin/bash\ntouch {fake_bin} && chmod +x {fake_bin}",
        )

        # 1. Discovery: broken
        invalidate_skills_cache()
        skills = discover_skills(skills_dir)
        assert skills[0]["healthy"] is False

        # 2. Planner sees broken annotation
        skill_list = build_planner_skill_list(skills, "admin")
        assert "[BROKEN" in skill_list

        # 3. Auto-repair on startup
        repaired = await repair_unhealthy_skills(skills_dir)
        assert "my-skill" in repaired
        assert fake_bin.exists()

        # 4. Re-discovery: healthy
        invalidate_skills_cache()
        env_path = os.environ.get("PATH", "") + ":" + str(bin_dir)
        with patch.dict(os.environ, {"PATH": env_path}):
            skills = discover_skills(skills_dir)

        assert skills[0]["healthy"] is True

        # 5. Planner sees clean skill
        skill_list = build_planner_skill_list(skills, "admin")
        assert "[BROKEN" not in skill_list
        assert "- my-skill — My tool" in skill_list
