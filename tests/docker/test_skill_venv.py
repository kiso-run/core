"""Docker integration tests for skill venv binary detection.

Verifies that check_deps() and build_skill_env() correctly find
pip-installed CLIs inside a skill's .venv/bin/ directory.

Run inside the dev container:
    docker compose -f docker-compose.test.yml --profile docker run --rm test-docker
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.skills import build_skill_env, check_deps


@pytest.fixture()
def skill_with_venv_binary(tmp_path):
    """Create a minimal skill dir with a fake binary in .venv/bin/."""
    skill_dir = tmp_path / "test-skill"
    venv_bin = skill_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    fake_bin = venv_bin / "fake-tool"
    fake_bin.write_text("#!/bin/sh\necho ok\n")
    fake_bin.chmod(0o755)

    return {
        "name": "test-skill",
        "path": str(skill_dir),
        "deps": {"bin": ["fake-tool"]},
        "env": {},
        "summary": "test skill",
    }


class TestCheckDepsVenv:
    def test_venv_binary_found(self, skill_with_venv_binary):
        """check_deps finds binary in .venv/bin/ even if not on system PATH."""
        missing = check_deps(skill_with_venv_binary)
        assert missing == []

    def test_missing_binary_reported(self, skill_with_venv_binary):
        """Binary not in venv or system PATH is reported as missing."""
        skill_with_venv_binary["deps"]["bin"] = ["nonexistent-xyz-binary"]
        missing = check_deps(skill_with_venv_binary)
        assert "nonexistent-xyz-binary" in missing

    def test_mixed_found_and_missing(self, skill_with_venv_binary):
        """Some binaries found in venv, others missing."""
        skill_with_venv_binary["deps"]["bin"] = ["fake-tool", "nonexistent-xyz"]
        missing = check_deps(skill_with_venv_binary)
        assert missing == ["nonexistent-xyz"]


class TestBuildSkillEnvVenv:
    """Verify build_skill_env includes .venv/bin/ in PATH."""

    def test_venv_bin_in_path(self, skill_with_venv_binary):
        """build_skill_env includes .venv/bin/ at the start of PATH."""
        env = build_skill_env(skill_with_venv_binary)
        venv_bin = str(Path(skill_with_venv_binary["path"]) / ".venv" / "bin")
        assert env["PATH"].startswith(venv_bin)

    def test_system_path_preserved(self, skill_with_venv_binary):
        """System PATH entries are preserved after venv bin."""
        env = build_skill_env(skill_with_venv_binary)
        path_parts = env["PATH"].split(":")
        assert len(path_parts) >= 2

    def test_no_venv_without_skill_path(self):
        """Skill without path gets system PATH only."""
        skill = {"name": "no-path", "path": "", "deps": {}, "env": {}}
        env = build_skill_env(skill)
        # PATH should not start with a skill venv prefix
        assert not env["PATH"].startswith("/.venv/bin")
        assert env["PATH"]  # system PATH still present
