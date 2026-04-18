"""Tests for kiso/skill_loader.py — Agent Skills discovery and parsing.

Business requirement: discover_skills() reads standard Agent Skills from
a directory tree, returns Skill dataclasses with all standard frontmatter
fields (name, description, license, compatibility, metadata,
allowed_tools) plus Kiso extension fields (when_to_use, audiences,
activation_hints, version), raw body, bundled root path, and role
sections extracted from ## Planner / ## Worker / ## Reviewer /
## Messenger headings.

Validates skill names per Agent Skills standard: lowercase, hyphens
only, 1-64 characters.

Cache is TTL-based (30s parity with recipe_loader).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.skill_loader import (
    Skill,
    discover_skills,
    invalidate_skills_cache,
    parse_skill_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_STANDARD_SKILL_BODY = """\
---
name: python-debug
description: Helps debug Python exceptions and stack traces.
license: MIT
compatibility: ">=1.0"
metadata:
  author: test
allowed-tools: Bash(python -m pytest *) Read
when_to_use: User reports a Python traceback or import error.
audiences: [planner, worker]
activation_hints:
  applies_to: [python, traceback, exception]
  excludes: [javascript]
version: "0.1.0"
---

Read the traceback, isolate the failing frame, propose a fix.

## Planner
Break the bug into: reproduce, isolate, fix, verify.

## Worker
Run `python -m pytest -x -q` and capture stderr.

## Reviewer
Output includes a diff or passing test log.
"""


_MINIMAL_SKILL_BODY = """\
---
name: terse
description: Be brief.
---

Respond in one sentence.
"""


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_skills_cache()
    yield
    invalidate_skills_cache()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_empty_dir_returns_empty_list(self, tmp_path):
        assert discover_skills(tmp_path) == []

    def test_missing_dir_returns_empty_list(self, tmp_path):
        assert discover_skills(tmp_path / "nope") == []

    def test_directory_skill_found(self, tmp_path):
        skill_dir = tmp_path / "python-debug"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(_STANDARD_SKILL_BODY)
        skills = discover_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "python-debug"

    def test_single_file_skill_found(self, tmp_path):
        # Single-file fallback: <name>.md at the skills root.
        (tmp_path / "terse.md").write_text(_MINIMAL_SKILL_BODY)
        skills = discover_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "terse"

    def test_directory_and_single_file_coexist(self, tmp_path):
        d = tmp_path / "python-debug"
        d.mkdir()
        (d / "SKILL.md").write_text(_STANDARD_SKILL_BODY)
        (tmp_path / "terse.md").write_text(_MINIMAL_SKILL_BODY)
        skills = discover_skills(tmp_path)
        names = sorted(s.name for s in skills)
        assert names == ["python-debug", "terse"]

    def test_malformed_frontmatter_skipped(self, tmp_path, caplog):
        (tmp_path / "bad.md").write_text("no frontmatter here\n")
        assert discover_skills(tmp_path) == []

    def test_unclosed_frontmatter_skipped(self, tmp_path):
        (tmp_path / "bad.md").write_text("---\nname: bad\n(no closing)\n")
        assert discover_skills(tmp_path) == []

    def test_missing_name_skipped(self, tmp_path):
        (tmp_path / "bad.md").write_text(
            "---\ndescription: no name\n---\nbody\n"
        )
        assert discover_skills(tmp_path) == []

    def test_missing_description_skipped(self, tmp_path):
        (tmp_path / "bad.md").write_text(
            "---\nname: foo\n---\nbody\n"
        )
        assert discover_skills(tmp_path) == []


# ---------------------------------------------------------------------------
# Naming validation (Agent Skills standard: lowercase, hyphens, 1-64 chars)
# ---------------------------------------------------------------------------


class TestNamingValidation:
    def test_valid_name_accepted(self, tmp_path):
        (tmp_path / "foo-bar.md").write_text(
            "---\nname: foo-bar\ndescription: ok\n---\n"
        )
        skills = discover_skills(tmp_path)
        assert len(skills) == 1

    def test_uppercase_rejected(self, tmp_path):
        (tmp_path / "bad.md").write_text(
            "---\nname: Foo\ndescription: x\n---\n"
        )
        assert discover_skills(tmp_path) == []

    def test_underscore_rejected(self, tmp_path):
        (tmp_path / "bad.md").write_text(
            "---\nname: foo_bar\ndescription: x\n---\n"
        )
        assert discover_skills(tmp_path) == []

    def test_spaces_rejected(self, tmp_path):
        (tmp_path / "bad.md").write_text(
            "---\nname: foo bar\ndescription: x\n---\n"
        )
        assert discover_skills(tmp_path) == []

    def test_over_64_chars_rejected(self, tmp_path):
        long_name = "a" * 65
        (tmp_path / "bad.md").write_text(
            f"---\nname: {long_name}\ndescription: x\n---\n"
        )
        assert discover_skills(tmp_path) == []

    def test_exactly_64_chars_accepted(self, tmp_path):
        name = "a" * 64
        (tmp_path / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: x\n---\n"
        )
        assert len(discover_skills(tmp_path)) == 1

    def test_starts_with_hyphen_rejected(self, tmp_path):
        (tmp_path / "bad.md").write_text(
            "---\nname: -foo\ndescription: x\n---\n"
        )
        assert discover_skills(tmp_path) == []


# ---------------------------------------------------------------------------
# Frontmatter fields
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def _load_one(self, tmp_path: Path) -> Skill:
        d = tmp_path / "python-debug"
        d.mkdir()
        (d / "SKILL.md").write_text(_STANDARD_SKILL_BODY)
        skills = discover_skills(tmp_path)
        assert len(skills) == 1
        return skills[0]

    def test_standard_fields_preserved(self, tmp_path):
        skill = self._load_one(tmp_path)
        assert skill.name == "python-debug"
        assert skill.description.startswith("Helps debug")
        assert skill.license == "MIT"
        assert skill.compatibility == ">=1.0"
        assert skill.metadata == {"author": "test"}
        assert skill.allowed_tools == "Bash(python -m pytest *) Read"

    def test_kiso_extensions_preserved(self, tmp_path):
        skill = self._load_one(tmp_path)
        assert "Python traceback" in skill.when_to_use
        assert skill.audiences == ["planner", "worker"]
        assert skill.activation_hints == {
            "applies_to": ["python", "traceback", "exception"],
            "excludes": ["javascript"],
        }
        assert skill.version == "0.1.0"

    def test_optional_fields_default(self, tmp_path):
        (tmp_path / "minimal.md").write_text(_MINIMAL_SKILL_BODY)
        skill = discover_skills(tmp_path)[0]
        assert skill.name == "terse"
        assert skill.description == "Be brief."
        assert skill.license is None
        assert skill.compatibility is None
        assert skill.metadata is None
        assert skill.allowed_tools is None
        assert skill.when_to_use is None
        assert skill.audiences is None
        assert skill.activation_hints is None
        assert skill.version is None


# ---------------------------------------------------------------------------
# Role sections
# ---------------------------------------------------------------------------


class TestRoleSections:
    def test_role_sections_extracted(self, tmp_path):
        d = tmp_path / "python-debug"
        d.mkdir()
        (d / "SKILL.md").write_text(_STANDARD_SKILL_BODY)
        skill = discover_skills(tmp_path)[0]
        sections = skill.role_sections
        assert "planner" in sections
        assert "worker" in sections
        assert "reviewer" in sections
        assert "Break the bug" in sections["planner"]
        assert "pytest" in sections["worker"]
        assert "diff" in sections["reviewer"]
        # messenger absent
        assert "messenger" not in sections

    def test_body_without_headings_has_no_role_sections(self, tmp_path):
        (tmp_path / "terse.md").write_text(_MINIMAL_SKILL_BODY)
        skill = discover_skills(tmp_path)[0]
        assert skill.role_sections == {}
        # Raw body still preserved.
        assert "one sentence" in skill.body

    def test_role_heading_case_insensitive(self, tmp_path):
        body = (
            "---\nname: foo\ndescription: x\n---\n\n"
            "Intro.\n\n## PLANNER\n\nplanner content\n\n"
            "## Worker\n\nworker content\n"
        )
        (tmp_path / "foo.md").write_text(body)
        skill = discover_skills(tmp_path)[0]
        assert "planner content" in skill.role_sections["planner"]
        assert "worker content" in skill.role_sections["worker"]

    def test_unknown_heading_not_captured(self, tmp_path):
        body = (
            "---\nname: foo\ndescription: x\n---\n\n"
            "## Random\n\nblah\n\n## Planner\n\nplanner content\n"
        )
        (tmp_path / "foo.md").write_text(body)
        skill = discover_skills(tmp_path)[0]
        assert set(skill.role_sections.keys()) == {"planner"}


# ---------------------------------------------------------------------------
# Bundled root path
# ---------------------------------------------------------------------------


class TestBundledRoot:
    def test_directory_skill_has_bundled_root(self, tmp_path):
        d = tmp_path / "python-debug"
        d.mkdir()
        (d / "SKILL.md").write_text(_STANDARD_SKILL_BODY)
        (d / "scripts").mkdir()
        (d / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")
        skill = discover_skills(tmp_path)[0]
        assert skill.bundled_root == d
        assert (skill.bundled_root / "scripts" / "run.sh").is_file()

    def test_single_file_skill_bundled_root_is_file_parent(self, tmp_path):
        (tmp_path / "terse.md").write_text(_MINIMAL_SKILL_BODY)
        skill = discover_skills(tmp_path)[0]
        # Single-file skills have no bundled assets; bundled_root is None.
        assert skill.bundled_root is None


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_invalidated_on_call(self, tmp_path):
        # First call: one skill.
        (tmp_path / "a.md").write_text(
            "---\nname: a\ndescription: x\n---\nbody\n"
        )
        assert len(discover_skills(tmp_path)) == 1
        # Add a new file.
        (tmp_path / "b.md").write_text(
            "---\nname: b\ndescription: y\n---\nbody\n"
        )
        # Without invalidation, cache returns stale.
        assert len(discover_skills(tmp_path)) == 1
        invalidate_skills_cache()
        assert len(discover_skills(tmp_path)) == 2


# ---------------------------------------------------------------------------
# parse_skill_file (single-file parser, exposed for CLI uses)
# ---------------------------------------------------------------------------


class TestParseSkillFile:
    def test_returns_skill_or_none(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("---\nname: x\ndescription: y\n---\nbody\n")
        skill = parse_skill_file(p)
        assert isinstance(skill, Skill)
        assert skill.name == "x"

    def test_returns_none_on_invalid(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("no frontmatter")
        assert parse_skill_file(p) is None
