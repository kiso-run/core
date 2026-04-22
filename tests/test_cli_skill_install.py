"""CLI integration tests for ``kiso skill install``.

Verifies argparse wiring, dispatch, dry-run semantics, and that the
real installer is called with the correct injected fetchers in the
non-dry-run path.
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


class TestArgparse:
    def test_install_subcommand_parses(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_skill.add_subcommands(parser)
        args = parser.parse_args(
            ["install", "--from-url", "https://github.com/acme/x"]
        )
        assert args.skill_command == "install"
        assert args.from_url == "https://github.com/acme/x"
        assert args.name is None
        assert args.dry_run is False

    def test_install_with_name_and_dry_run(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_skill.add_subcommands(parser)
        args = parser.parse_args(
            [
                "install",
                "--from-url", "https://github.com/acme/x",
                "--name", "custom",
                "--dry-run",
            ]
        )
        assert args.dry_run is True
        assert args.name == "custom"

    def test_install_missing_from_url_rejected(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_skill.add_subcommands(parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["install"])


class TestDryRun:
    def test_dry_run_prints_plan_no_side_effects(self, skills_dir, capsys):
        rc = cli_skill._cmd_install(
            _ns(
                from_url="https://github.com/acme/python-debug",
                name=None,
                dry_run=True,
                yes=False,
                force=False,
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "python-debug" in out
        assert "github_repo" in out or "github" in out.lower()
        # No skill was written
        assert list(skills_dir.iterdir()) == []


class TestExecution:
    def test_install_raw_md_via_injected_fetcher(
        self, skills_dir, monkeypatch, capsys
    ):
        captured_url = []

        def fake_fetch_text(url: str) -> str:
            captured_url.append(url)
            return _STANDARD_SKILL

        monkeypatch.setattr(cli_skill, "_http_fetcher", fake_fetch_text)
        rc = cli_skill._cmd_install(
            _ns(
                from_url="https://ex.com/x/SKILL.md",
                name=None,
                dry_run=False,
                yes=True,
                force=False,
            )
        )
        assert rc == 0
        assert captured_url == ["https://ex.com/x/SKILL.md"]
        target = skills_dir / "python-debug" / "SKILL.md"
        assert target.exists()
        # Provenance file accompanies the install.
        assert (skills_dir / "python-debug" / ".provenance.json").exists()


class TestTopLevelWiring:
    def test_top_level_parser_includes_install(self):
        from cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            ["skill", "install", "--from-url", "https://github.com/a/b"]
        )
        assert args.command == "skill"
        assert args.skill_command == "install"
        assert args.from_url == "https://github.com/a/b"
