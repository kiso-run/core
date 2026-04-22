"""Tests for ``cli/skill.py`` — the ``kiso skill`` subcommand.

Business requirement: users manage skills installed under
``~/.kiso/skills/`` with four local-only subcommands — ``list``, ``info``,
``add``, ``remove`` — that mirror the ergonomics of ``kiso mcp``.

URL-based install and ``kiso skill test`` are intentionally out of scope
here; they land in later milestones.

``add`` accepts either a directory (with ``SKILL.md``) or a single
``.md`` file, validates frontmatter + Agent Skills naming convention via
``parse_skill_file``, and copies into the skills directory. ``info``
renders metadata and role-scoped sections so users can inspect exactly
what the planner / worker / reviewer / messenger will see.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from cli import skill as cli_skill


_STANDARD_SKILL = """\
---
name: python-debug
description: Helps debug Python exceptions and stack traces.
license: MIT
when_to_use: User reports a Python traceback or import error.
version: "0.1.0"
---

Read the traceback, isolate the failing frame, propose a fix.

## Planner
Break the bug into reproduce, isolate, fix, verify.

## Worker
Run `python -m pytest -x -q` and capture stderr.
"""

_MINIMAL_SKILL = """\
---
name: terse
description: Be brief.
---

Respond in one sentence.
"""

_BAD_NAME_SKILL = """\
---
name: Bad-Name
description: uppercase letters in the name are not allowed.
---

body
"""

_NO_FRONTMATTER = "no frontmatter here\n"


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    """Point ``cli.skill`` at a tmp skills directory."""
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(cli_skill, "SKILLS_DIR", d)
    # The loader caches by directory; clear it so each test starts fresh.
    from kiso.skill_loader import invalidate_skills_cache

    invalidate_skills_cache()
    yield d
    invalidate_skills_cache()


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _write_dir_skill(root: Path, name: str, body: str = _STANDARD_SKILL) -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(body)
    return d


def _write_file_skill(root: Path, name: str, body: str = _MINIMAL_SKILL) -> Path:
    p = root / f"{name}.md"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_lists_nothing(self, skills_dir, capsys):
        assert cli_skill._cmd_list() == 0
        out = capsys.readouterr().out
        assert "no skills" in out.lower()

    def test_shows_installed_skills(self, skills_dir, capsys):
        _write_dir_skill(skills_dir, "python-debug")
        _write_file_skill(skills_dir, "terse")
        assert cli_skill._cmd_list() == 0
        out = capsys.readouterr().out
        assert "python-debug" in out
        assert "terse" in out
        # Descriptions must surface so the table is useful.
        assert "Helps debug Python" in out
        assert "Be brief" in out

    def test_distinguishes_directory_vs_file_source(self, skills_dir, capsys):
        _write_dir_skill(skills_dir, "python-debug")
        _write_file_skill(skills_dir, "terse")
        assert cli_skill._cmd_list() == 0
        out = capsys.readouterr().out
        # Source kind column lets users tell a dir skill from a file skill.
        assert "directory" in out
        assert "file" in out


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


class TestInfo:
    def test_info_unknown_skill_dies(self, skills_dir):
        with pytest.raises(SystemExit):
            cli_skill._cmd_info(_ns(name="does-not-exist"))

    def test_info_shows_metadata(self, skills_dir, capsys):
        _write_dir_skill(skills_dir, "python-debug")
        rc = cli_skill._cmd_info(_ns(name="python-debug"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "python-debug" in out
        assert "Helps debug Python" in out
        # Optional standard fields should appear when present
        assert "MIT" in out  # license
        assert "0.1.0" in out  # version
        assert "traceback" in out.lower()  # when_to_use

    def test_info_renders_role_sections_with_headers(self, skills_dir, capsys):
        _write_dir_skill(skills_dir, "python-debug")
        rc = cli_skill._cmd_info(_ns(name="python-debug"))
        assert rc == 0
        out = capsys.readouterr().out
        # Both role sections must appear under clearly-labeled headers
        # so the user can see what planner/worker will be fed.
        assert "Planner" in out
        assert "reproduce, isolate, fix, verify" in out
        assert "Worker" in out
        assert "pytest" in out


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestAdd:
    def test_add_directory_skill_copies_whole_tree(self, skills_dir, tmp_path):
        src = tmp_path / "src_python-debug"
        src.mkdir()
        (src / "SKILL.md").write_text(_STANDARD_SKILL)
        (src / "scripts").mkdir()
        (src / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")

        rc = cli_skill._cmd_add(_ns(path=str(src), yes=False))
        assert rc == 0

        target = skills_dir / "python-debug"
        assert target.is_dir()
        assert (target / "SKILL.md").read_text() == _STANDARD_SKILL
        assert (target / "scripts" / "run.sh").exists()

    def test_add_single_md_file_copies_as_name_md(self, skills_dir, tmp_path):
        src = tmp_path / "anywhere.md"
        src.write_text(_MINIMAL_SKILL)

        rc = cli_skill._cmd_add(_ns(path=str(src), yes=False))
        assert rc == 0

        target = skills_dir / "terse.md"
        assert target.is_file()
        assert target.read_text() == _MINIMAL_SKILL

    def test_add_rejects_bad_frontmatter(self, skills_dir, tmp_path):
        src = tmp_path / "broken.md"
        src.write_text(_NO_FRONTMATTER)
        with pytest.raises(SystemExit):
            cli_skill._cmd_add(_ns(path=str(src), yes=False))
        # Nothing should have been copied
        assert list(skills_dir.iterdir()) == []

    def test_add_rejects_invalid_name(self, skills_dir, tmp_path):
        src = tmp_path / "Bad.md"
        src.write_text(_BAD_NAME_SKILL)
        with pytest.raises(SystemExit):
            cli_skill._cmd_add(_ns(path=str(src), yes=False))
        assert list(skills_dir.iterdir()) == []

    def test_add_rejects_directory_without_skill_md(self, skills_dir, tmp_path):
        src = tmp_path / "no_skill_md"
        src.mkdir()
        (src / "README.md").write_text("# nothing useful\n")
        with pytest.raises(SystemExit):
            cli_skill._cmd_add(_ns(path=str(src), yes=False))
        assert list(skills_dir.iterdir()) == []

    def test_add_refuses_overwrite_without_yes(self, skills_dir, tmp_path):
        _write_dir_skill(skills_dir, "python-debug")
        src = tmp_path / "src_python-debug"
        src.mkdir()
        (src / "SKILL.md").write_text(_STANDARD_SKILL)
        with pytest.raises(SystemExit):
            cli_skill._cmd_add(_ns(path=str(src), yes=False))

    def test_add_overwrites_with_yes(self, skills_dir, tmp_path):
        _write_dir_skill(skills_dir, "python-debug", body=_STANDARD_SKILL)
        # A slightly different body so we can detect the overwrite.
        updated = _STANDARD_SKILL.replace("0.1.0", "0.2.0")
        src = tmp_path / "src_python-debug"
        src.mkdir()
        (src / "SKILL.md").write_text(updated)
        rc = cli_skill._cmd_add(_ns(path=str(src), yes=True))
        assert rc == 0
        assert "0.2.0" in (skills_dir / "python-debug" / "SKILL.md").read_text()

    def test_add_missing_source_dies(self, skills_dir, tmp_path):
        with pytest.raises(SystemExit):
            cli_skill._cmd_add(_ns(path=str(tmp_path / "nope"), yes=False))


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_directory_skill(self, skills_dir):
        _write_dir_skill(skills_dir, "python-debug")
        rc = cli_skill._cmd_remove(_ns(name="python-debug", yes=True))
        assert rc == 0
        assert not (skills_dir / "python-debug").exists()

    def test_remove_single_file_skill(self, skills_dir):
        _write_file_skill(skills_dir, "terse")
        rc = cli_skill._cmd_remove(_ns(name="terse", yes=True))
        assert rc == 0
        assert not (skills_dir / "terse.md").exists()

    def test_remove_unknown_dies(self, skills_dir):
        with pytest.raises(SystemExit):
            cli_skill._cmd_remove(_ns(name="does-not-exist", yes=True))


# ---------------------------------------------------------------------------
# Argparse wiring + dispatcher
# ---------------------------------------------------------------------------


class TestWiring:
    def test_subcommands_registered(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_skill.add_subcommands(parser)
        # Each subcommand must be parseable through the top-level parser.
        for argv in (
            ["list"],
            ["info", "python-debug"],
            ["add", "/tmp/x"],
            ["remove", "python-debug"],
        ):
            args = parser.parse_args(argv)
            assert args.skill_command == argv[0]

    def test_handle_dispatches_list(self, skills_dir, capsys, monkeypatch):
        called = []
        monkeypatch.setattr(cli_skill, "_cmd_list", lambda: called.append("list") or 0)
        rc = cli_skill.handle(_ns(skill_command="list"))
        assert rc == 0
        assert called == ["list"]

    def test_handle_dispatches_info_add_remove(self, skills_dir, monkeypatch):
        seen = []
        monkeypatch.setattr(cli_skill, "_cmd_info", lambda a: seen.append(("info", a)) or 0)
        monkeypatch.setattr(cli_skill, "_cmd_add", lambda a: seen.append(("add", a)) or 0)
        monkeypatch.setattr(cli_skill, "_cmd_remove", lambda a: seen.append(("remove", a)) or 0)
        assert cli_skill.handle(_ns(skill_command="info", name="x")) == 0
        assert cli_skill.handle(_ns(skill_command="add", path="/tmp/x", yes=False)) == 0
        assert cli_skill.handle(_ns(skill_command="remove", name="x", yes=True)) == 0
        assert [s[0] for s in seen] == ["info", "add", "remove"]

    def test_handle_no_subcommand_usage(self, skills_dir, capsys):
        rc = cli_skill.handle(_ns(skill_command=None))
        assert rc == 2
        out = capsys.readouterr().out
        assert "usage" in out.lower()


class TestCLIIntegration:
    """`kiso skill …` must be reachable via the top-level parser."""

    def test_top_level_parser_includes_skill(self):
        from cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["skill", "list"])
        assert args.command == "skill"
        assert args.skill_command == "list"
