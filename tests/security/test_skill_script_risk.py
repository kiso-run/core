"""Concern 2 — skill scripts are flagged as a risk factor at install time.

The skill trust machinery classifies a source (tier1 / custom /
untrusted), but trust applies to the *instructions* the skill ships.
A skill bundling executable content in ``scripts/`` must always be
surfaced as a risk factor so an operator sees it before approval —
trusted-as-instructions does not imply trusted-as-code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.skill_trust import detect_risk_factors


def _write_skill(
    dir_path: Path,
    *,
    name: str = "demo",
    description: str = "Demo skill for tests.",
) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody\n",
        encoding="utf-8",
    )
    return dir_path


class TestScriptsDirectoryAlwaysRiskFlagged:
    def test_empty_skill_has_no_risks(self, tmp_path):
        skill = _write_skill(tmp_path / "clean")
        assert detect_risk_factors(skill) == []

    def test_non_empty_scripts_dir_is_a_risk(self, tmp_path):
        skill = _write_skill(tmp_path / "with_scripts")
        scripts = skill / "scripts"
        scripts.mkdir()
        (scripts / "install.sh").write_text("#!/bin/sh\necho hi\n")
        risks = detect_risk_factors(skill)
        assert any("scripts" in r for r in risks), risks

    def test_empty_scripts_dir_is_not_a_risk(self, tmp_path):
        # Only non-empty scripts/ counts — an empty directory is
        # effectively absent.
        skill = _write_skill(tmp_path / "empty_scripts")
        (skill / "scripts").mkdir()
        assert detect_risk_factors(skill) == []

    def test_risk_flag_independent_of_trust_tier(self, tmp_path):
        # Risk detection does not read the trust tier at all —
        # even a skill that the caller later classifies as tier1
        # still surfaces its script risk. (Verified indirectly by
        # detect_risk_factors having no trust-tier parameter.)
        skill = _write_skill(tmp_path / "tier1_like")
        (skill / "scripts").mkdir()
        (skill / "scripts" / "go.py").write_text("print('x')\n")
        risks = detect_risk_factors(skill)
        assert any("scripts" in r for r in risks), risks
