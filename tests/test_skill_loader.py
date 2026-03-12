"""M449: Tests for kiso.skill_loader — MD-based skill discovery and parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.skill_loader import (
    build_planner_skill_list,
    discover_md_skills,
    invalidate_md_skills_cache,
    _parse_skill_file,
)


_VALID_SKILL = """\
---
name: data-analyst
summary: Guides planner for data analysis tasks
---

When the user asks about data analysis:
- Prefer pandas for tabular data
- Use matplotlib for charts
"""

_NO_FRONTMATTER = """\
Just a plain markdown file with no frontmatter.
"""

_MISSING_NAME = """\
---
summary: No name field
---

Instructions here.
"""

_MISSING_SUMMARY = """\
---
name: missing-summary
---

Instructions here.
"""

_EMPTY_BODY = """\
---
name: minimal
summary: Minimal skill with no instructions
---
"""


class TestParseSkillFile:
    def test_valid_skill(self, tmp_path):
        f = tmp_path / "data-analyst.md"
        f.write_text(_VALID_SKILL)
        result = _parse_skill_file(f)
        assert result is not None
        assert result["name"] == "data-analyst"
        assert result["summary"] == "Guides planner for data analysis tasks"
        assert "pandas" in result["instructions"]
        assert result["path"] == str(f)

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text(_NO_FRONTMATTER)
        assert _parse_skill_file(f) is None

    def test_missing_name(self, tmp_path):
        f = tmp_path / "noname.md"
        f.write_text(_MISSING_NAME)
        assert _parse_skill_file(f) is None

    def test_missing_summary(self, tmp_path):
        f = tmp_path / "nosummary.md"
        f.write_text(_MISSING_SUMMARY)
        assert _parse_skill_file(f) is None

    def test_empty_body(self, tmp_path):
        f = tmp_path / "minimal.md"
        f.write_text(_EMPTY_BODY)
        result = _parse_skill_file(f)
        assert result is not None
        assert result["name"] == "minimal"
        assert result["instructions"] == ""

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nonexistent.md"
        assert _parse_skill_file(f) is None

    def test_unclosed_frontmatter(self, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("---\nname: bad\nsummary: bad\nNo closing marker")
        assert _parse_skill_file(f) is None


class TestDiscoverMdSkills:
    def test_discovers_valid_skills(self, tmp_path):
        (tmp_path / "skill-a.md").write_text(_VALID_SKILL)
        (tmp_path / "skill-b.md").write_text(_EMPTY_BODY.replace("minimal", "skill-b").replace(
            "Minimal skill with no instructions", "Second skill"))
        invalidate_md_skills_cache()
        skills = discover_md_skills(tmp_path)
        assert len(skills) == 2
        names = {s["name"] for s in skills}
        assert "data-analyst" in names
        assert "skill-b" in names

    def test_skips_invalid_files(self, tmp_path):
        (tmp_path / "valid.md").write_text(_VALID_SKILL)
        (tmp_path / "invalid.md").write_text(_NO_FRONTMATTER)
        invalidate_md_skills_cache()
        skills = discover_md_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0]["name"] == "data-analyst"

    def test_empty_directory(self, tmp_path):
        invalidate_md_skills_cache()
        skills = discover_md_skills(tmp_path)
        assert skills == []

    def test_nonexistent_directory(self, tmp_path):
        invalidate_md_skills_cache()
        skills = discover_md_skills(tmp_path / "nonexistent")
        assert skills == []

    def test_ignores_non_md_files(self, tmp_path):
        (tmp_path / "valid.md").write_text(_VALID_SKILL)
        (tmp_path / "readme.txt").write_text("not a skill")
        (tmp_path / "script.py").write_text("pass")
        invalidate_md_skills_cache()
        skills = discover_md_skills(tmp_path)
        assert len(skills) == 1

    def test_caching(self, tmp_path):
        (tmp_path / "skill.md").write_text(_VALID_SKILL)
        invalidate_md_skills_cache()
        first = discover_md_skills(tmp_path)
        assert len(first) == 1
        # Add another file — cache should return old result
        (tmp_path / "new.md").write_text(_EMPTY_BODY)
        second = discover_md_skills(tmp_path)
        assert len(second) == 1  # still cached
        # Invalidate and re-discover
        invalidate_md_skills_cache()
        third = discover_md_skills(tmp_path)
        assert len(third) == 2

    def test_sorted_by_filename(self, tmp_path):
        (tmp_path / "z-skill.md").write_text(
            "---\nname: z-last\nsummary: Last\n---\nBody")
        (tmp_path / "a-skill.md").write_text(
            "---\nname: a-first\nsummary: First\n---\nBody")
        invalidate_md_skills_cache()
        skills = discover_md_skills(tmp_path)
        assert skills[0]["name"] == "a-first"
        assert skills[1]["name"] == "z-last"


class TestBuildPlannerSkillList:
    def test_empty_list(self):
        assert build_planner_skill_list([]) == ""

    def test_single_skill(self):
        skills = [{"name": "analyst", "summary": "Data analysis", "instructions": "Use pandas."}]
        result = build_planner_skill_list(skills)
        assert "- analyst — Data analysis" in result
        assert "  Use pandas." in result

    def test_multiple_skills(self):
        skills = [
            {"name": "analyst", "summary": "Data analysis", "instructions": "Use pandas."},
            {"name": "writer", "summary": "Writing style", "instructions": "Be concise."},
        ]
        result = build_planner_skill_list(skills)
        assert "- analyst — Data analysis" in result
        assert "- writer — Writing style" in result

    def test_no_instructions(self):
        skills = [{"name": "minimal", "summary": "Minimal", "instructions": ""}]
        result = build_planner_skill_list(skills)
        assert result == "- minimal — Minimal"
