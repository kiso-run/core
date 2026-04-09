"""Tests for `kiso role reset` and `kiso role list` CLI commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def role_dir(tmp_path):
    """Per-test KISO_DIR with an empty roles/ subdir."""
    rd = tmp_path / "roles"
    rd.mkdir()
    return tmp_path


class TestRoleReset:
    """`kiso role reset NAME` and `--all` overwrite user role files
    from the package."""

    def test_reset_single_role_writes_package_content(self, role_dir, capsys):
        from cli.role import role_reset
        with patch("cli.role.KISO_DIR", role_dir):
            args = argparse.Namespace(name="planner", all=False, yes=True)
            role_reset(args)
        target = role_dir / "roles" / "planner.md"
        assert target.is_file()
        assert len(target.read_text()) > 0
        assert "planner" in target.read_text().lower()

    def test_reset_overwrites_existing_custom_content(self, role_dir, capsys):
        from cli.role import role_reset
        target = role_dir / "roles" / "planner.md"
        target.write_text("# my custom planner")
        with patch("cli.role.KISO_DIR", role_dir):
            args = argparse.Namespace(name="planner", all=False, yes=True)
            role_reset(args)
        # Now contains package content, not custom
        assert "# my custom planner" not in target.read_text()
        assert len(target.read_text()) > 100

    def test_reset_all_writes_every_package_role(self, role_dir, capsys):
        from cli.role import role_reset
        with patch("cli.role.KISO_DIR", role_dir):
            args = argparse.Namespace(name=None, all=True, yes=True)
            role_reset(args)
        roles_dest = role_dir / "roles"
        for role in ("planner", "reviewer", "messenger", "classifier",
                     "briefer", "curator", "paraphraser"):
            target = roles_dest / f"{role}.md"
            assert target.is_file(), f"{role}.md missing after reset --all"
            assert len(target.read_text()) > 0

    def test_reset_unknown_role_errors(self, role_dir, capsys):
        from cli.role import role_reset
        with patch("cli.role.KISO_DIR", role_dir):
            args = argparse.Namespace(name="not_a_real_role", all=False, yes=True)
            with pytest.raises(SystemExit):
                role_reset(args)
        captured = capsys.readouterr()
        assert "not_a_real_role" in (captured.err + captured.out)

    def test_reset_without_name_or_all_errors(self, role_dir, capsys):
        from cli.role import role_reset
        with patch("cli.role.KISO_DIR", role_dir):
            args = argparse.Namespace(name=None, all=False, yes=True)
            with pytest.raises(SystemExit):
                role_reset(args)

    def test_reset_all_does_not_touch_custom_only_files(self, role_dir, capsys):
        """A user file with no package counterpart is left alone."""
        from cli.role import role_reset
        custom = role_dir / "roles" / "my_custom.md"
        custom.write_text("# my custom only role")
        with patch("cli.role.KISO_DIR", role_dir):
            args = argparse.Namespace(name=None, all=True, yes=True)
            role_reset(args)
        # Custom-only file untouched
        assert custom.read_text() == "# my custom only role"


class TestRoleList:
    """`kiso role list` prints user roles vs package roles."""

    def test_list_shows_package_and_user_roles(self, role_dir, capsys):
        from cli.role import role_list
        # Pre-populate one user role
        (role_dir / "roles" / "planner.md").write_text("custom planner")
        with patch("cli.role.KISO_DIR", role_dir):
            role_list(argparse.Namespace())
        out = capsys.readouterr().out
        assert "planner" in out
        assert "reviewer" in out  # from package


class TestCLIIntegration:
    """`kiso role` is wired into the main CLI."""

    def test_kiso_role_subcommand_exists(self):
        from cli import build_parser
        parser = build_parser()
        # The parser should accept "role reset NAME"
        args = parser.parse_args(["role", "reset", "planner"])
        assert args.command == "role"
        assert args.role_command == "reset"
        assert args.name == "planner"

    def test_kiso_role_reset_all_flag(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["role", "reset", "--all"])
        assert args.command == "role"
        assert args.role_command == "reset"
        assert args.all is True

    def test_kiso_role_list_subcommand(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["role", "list"])
        assert args.command == "role"
        assert args.role_command == "list"
