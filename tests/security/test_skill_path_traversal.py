"""Concern 7 — a skill cannot be installed outside ``~/.kiso/skills/``.

The install path is ``SKILLS_DIR / <frontmatter.name>``. If the
frontmatter name contained ``..`` or a ``/`` separator, the join
would escape the target root. The loader's name validation is the
defence in depth that prevents this — any malicious frontmatter is
rejected as "not a valid Agent Skill" before installation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.skill_loader import parse_skill_file


def _write(path: Path, frontmatter_name: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {frontmatter_name}\n"
        "description: Attempts a path traversal at install time.\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    return path


class TestNameTraversalRejected:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "../../evil",
            "../escape",
            "evil/../sibling",
            "/absolute",
            "with/slash",
            "with space",
            "UPPER",
            "_leading-underscore",
            "-leading-hyphen",
            "",
        ],
    )
    def test_bad_name_rejected(self, tmp_path, bad_name):
        skill_md = _write(tmp_path / "SKILL.md", bad_name)
        assert parse_skill_file(skill_md) is None

    def test_valid_name_accepted(self, tmp_path):
        skill_md = _write(tmp_path / "SKILL.md", "example-skill")
        parsed = parse_skill_file(skill_md)
        assert parsed is not None
        assert parsed.name == "example-skill"

    def test_name_regex_blocks_dotdot_inside(self, tmp_path):
        skill_md = _write(tmp_path / "SKILL.md", "foo..bar")
        assert parse_skill_file(skill_md) is None
