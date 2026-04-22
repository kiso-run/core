"""Tests for ``kiso skill test <name>`` — local skill audit.

Business requirement: ``kiso skill test`` audits an installed
skill against the same rules the runtime enforces, surfaces risks
the user should know about, and exits non-zero on hard failures.

Hard failures (exit 1):
- frontmatter is missing / malformed
- required fields (``name``, ``description``) absent or empty
- skill name violates the Agent Skills naming rule
- role sections are declared but the body has none when at least
  one ``## Planner`` / ``## Worker`` / ``## Reviewer`` /
  ``## Messenger`` heading exists (treated as malformed — the
  body must have content under each declared role)

Warnings (still exit 0):
- markdown link points at a path that does not exist
- ``allowed-tools`` references an external command not on PATH
"""

from __future__ import annotations

import argparse

import pytest

from cli import skill as cli_skill


_STANDARD_SKILL = """\
---
name: python-debug
description: Helps debug Python tracebacks.
---

Read the traceback, isolate the failing frame, propose a fix.

## Planner
Reproduce, isolate, fix, verify.

## Worker
Run `python -m pytest -x -q`.
"""

_BAD_FRONTMATTER = "no frontmatter\n"

_MISSING_DESCRIPTION = """\
---
name: broken
---

body
"""


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(cli_skill, "SKILLS_DIR", d)
    from kiso.skill_loader import invalidate_skills_cache
    invalidate_skills_cache()
    return d


def _write_dir_skill(root, name, body=_STANDARD_SKILL, extra_files=None):
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(body)
    for p, content in (extra_files or {}).items():
        target = d / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return d


class TestArgparse:
    def test_test_subcommand_parses(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_skill.add_subcommands(parser)
        args = parser.parse_args(["test", "python-debug"])
        assert args.skill_command == "test"
        assert args.name == "python-debug"


class TestHappyPath:
    def test_clean_skill_passes(self, skills_dir, capsys):
        _write_dir_skill(skills_dir, "python-debug")
        rc = cli_skill._cmd_test(_ns(name="python-debug"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "python-debug" in out
        assert "ok" in out.lower() or "pass" in out.lower()


class TestHardFailures:
    def test_unknown_skill_exits_nonzero(self, skills_dir):
        with pytest.raises(SystemExit):
            cli_skill._cmd_test(_ns(name="does-not-exist"))

    def test_bad_frontmatter_exits_nonzero(self, skills_dir):
        _write_dir_skill(skills_dir, "broken", body=_BAD_FRONTMATTER)
        rc = cli_skill._cmd_test(_ns(name="broken"))
        assert rc != 0

    def test_missing_required_field_exits_nonzero(self, skills_dir):
        _write_dir_skill(skills_dir, "broken", body=_MISSING_DESCRIPTION)
        rc = cli_skill._cmd_test(_ns(name="broken"))
        assert rc != 0


class TestWarnings:
    def test_broken_relative_link_warns(self, skills_dir, capsys):
        body = _STANDARD_SKILL.replace(
            "Read the traceback, isolate the failing frame, propose a fix.",
            "See [reference](references/missing.md) for details.",
        )
        _write_dir_skill(skills_dir, "python-debug", body=body)
        rc = cli_skill._cmd_test(_ns(name="python-debug"))
        # Warning → still exit 0
        assert rc == 0
        out = capsys.readouterr().out
        assert "references/missing.md" in out

    def test_missing_allowed_tool_warns(self, skills_dir, capsys, monkeypatch):
        body = _STANDARD_SKILL.replace(
            "description: Helps debug Python tracebacks.",
            "description: Helps debug Python tracebacks.\n"
            "allowed-tools: Bash(nonexistent-binary-x93j *)",
        )
        _write_dir_skill(skills_dir, "python-debug", body=body)
        # Force shutil.which to say the binary is missing.
        import shutil as _sh
        monkeypatch.setattr(_sh, "which", lambda _name: None)
        rc = cli_skill._cmd_test(_ns(name="python-debug"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "nonexistent-binary-x93j" in out


class TestReferencedFiles:
    def test_valid_reference_passes(self, skills_dir):
        body = _STANDARD_SKILL.replace(
            "Read the traceback, isolate the failing frame, propose a fix.",
            "See [ref](references/guide.md) for context.",
        )
        _write_dir_skill(
            skills_dir,
            "python-debug",
            body=body,
            extra_files={"references/guide.md": "# guide\n"},
        )
        rc = cli_skill._cmd_test(_ns(name="python-debug"))
        assert rc == 0
