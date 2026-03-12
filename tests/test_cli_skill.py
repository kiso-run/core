"""M451: Tests for CLI skill commands (MD-based skill management)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.skill import (
    SKILLS_DIR,
    _skill_install,
    _skill_list,
    _skill_remove,
    run_skill_command,
)


_VALID_SKILL = """\
---
name: data-analyst
summary: Guides planner for data analysis tasks
---

When the user asks about data analysis:
- Prefer pandas for tabular data
"""


class _FakeArgs:
    """Minimal args namespace for testing."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestSkillList:
    def test_list_empty(self, tmp_path, capsys):
        with patch("cli.skill.SKILLS_DIR", tmp_path):
            _skill_list()
        assert "No skills installed" in capsys.readouterr().out

    def test_list_with_skills(self, tmp_path, capsys):
        (tmp_path / "data-analyst.md").write_text(_VALID_SKILL)
        with patch("cli.skill.SKILLS_DIR", tmp_path):
            from kiso.skill_loader import invalidate_md_skills_cache
            invalidate_md_skills_cache()
            _skill_list()
        out = capsys.readouterr().out
        assert "data-analyst" in out
        assert "Guides planner" in out


class TestSkillInstall:
    def test_install_valid(self, tmp_path, capsys):
        source = tmp_path / "src" / "my-skill.md"
        source.parent.mkdir()
        source.write_text(_VALID_SKILL)
        dest_dir = tmp_path / "skills"

        args = _FakeArgs(source=str(source))
        with patch("cli.skill.SKILLS_DIR", dest_dir):
            _skill_install(args)

        out = capsys.readouterr().out
        assert "installed" in out
        assert (dest_dir / "my-skill.md").exists()

    def test_install_nonexistent(self, tmp_path):
        args = _FakeArgs(source=str(tmp_path / "nope.md"))
        with pytest.raises(SystemExit):
            _skill_install(args)

    def test_install_invalid_file(self, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_text("no frontmatter here")
        args = _FakeArgs(source=str(bad))
        with pytest.raises(SystemExit):
            _skill_install(args)

    def test_install_non_md(self, tmp_path):
        txt = tmp_path / "skill.txt"
        txt.write_text(_VALID_SKILL)
        args = _FakeArgs(source=str(txt))
        with pytest.raises(SystemExit):
            _skill_install(args)

    def test_install_update_existing(self, tmp_path, capsys):
        dest_dir = tmp_path / "skills"
        dest_dir.mkdir()
        (dest_dir / "my-skill.md").write_text("old content")

        source = tmp_path / "my-skill.md"
        source.write_text(_VALID_SKILL)

        args = _FakeArgs(source=str(source))
        with patch("cli.skill.SKILLS_DIR", dest_dir):
            _skill_install(args)

        out = capsys.readouterr().out
        assert "updating" in out.lower()
        assert "installed" in out


class TestSkillRemove:
    def test_remove_by_name(self, tmp_path, capsys):
        dest_dir = tmp_path / "skills"
        dest_dir.mkdir()
        (dest_dir / "data-analyst.md").write_text(_VALID_SKILL)

        args = _FakeArgs(name="data-analyst")
        with patch("cli.skill.SKILLS_DIR", dest_dir):
            _skill_remove(args)

        out = capsys.readouterr().out
        assert "removed" in out.lower()
        assert not (dest_dir / "data-analyst.md").exists()

    def test_remove_nonexistent(self, tmp_path):
        dest_dir = tmp_path / "skills"
        dest_dir.mkdir()

        args = _FakeArgs(name="nope")
        with patch("cli.skill.SKILLS_DIR", dest_dir):
            with pytest.raises(SystemExit):
                _skill_remove(args)


class TestRunSkillCommand:
    def test_no_command(self):
        args = _FakeArgs(skill_command=None)
        with pytest.raises(SystemExit):
            run_skill_command(args)
