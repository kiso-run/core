"""End-to-end integration tests for the kiso user CLI subcommand.

These tests exercise the full main() → build_parser() → dispatch → function
pipeline instead of calling internal functions directly.  The only patches
used are:
  - sys.argv                    — to supply the command line without spawning a subprocess
  - cli.user.require_admin      — OS-level admin check (irrelevant to CLI correctness)
  - cli.user.CONFIG_PATH_DEFAULT — to point at a test-local config.toml
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._cli_user_helpers import make_user_config, read_users


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run(argv: list[str], config_path: Path) -> None:
    """Call main() with patched sys.argv and config path."""
    from cli import main

    with (
        patch.object(sys, "argv", ["kiso", *argv]),
        patch("cli.user.require_admin"),
        patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
    ):
        main()


# ---------------------------------------------------------------------------
# kiso user list
# ---------------------------------------------------------------------------

class TestIntegrationUserList:
    def test_list_shows_users(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        _run(["user", "list"], config_path)
        out = capsys.readouterr().out
        assert "boss" in out
        assert "admin" in out
        assert "alice" in out

    def test_list_json(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        _run(["user", "list", "--json"], config_path)
        data = json.loads(capsys.readouterr().out)
        assert data["boss"]["role"] == "admin"
        assert data["alice"]["skills"] == ["skill1", "skill2"]

    def test_list_no_subcommand_exits(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _run(["user"], config_path)
        assert exc.value.code == 1
        assert "usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# kiso user add
# ---------------------------------------------------------------------------

class TestIntegrationUserAdd:
    def test_add_admin_no_reload(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        _run(["user", "add", "newguy", "--role", "admin", "--no-reload"], config_path)
        users = read_users(config_path)
        assert "newguy" in users
        assert users["newguy"]["role"] == "admin"
        assert "added" in capsys.readouterr().out

    def test_add_user_with_skills_no_reload(self, tmp_path):
        config_path = make_user_config(tmp_path)
        _run(["user", "add", "bob", "--role", "user", "--skills", "read,write", "--no-reload"], config_path)
        assert read_users(config_path)["bob"]["skills"] == ["read", "write"]

    def test_add_existing_fails(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _run(["user", "add", "boss", "--role", "admin", "--no-reload"], config_path)
        assert exc.value.code == 1
        assert "already exists" in capsys.readouterr().out

    def test_add_invalid_username_fails(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _run(["user", "add", "INVALID!", "--role", "admin", "--no-reload"], config_path)
        assert exc.value.code == 1
        assert "invalid username" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# kiso user edit
# ---------------------------------------------------------------------------

class TestIntegrationUserEdit:
    def test_edit_role(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        _run(["user", "edit", "alice", "--role", "admin", "--no-reload"], config_path)
        assert read_users(config_path)["alice"]["role"] == "admin"
        assert "updated" in capsys.readouterr().out

    def test_edit_skills(self, tmp_path):
        config_path = make_user_config(tmp_path)
        _run(["user", "edit", "alice", "--skills", "x,y", "--no-reload"], config_path)
        assert read_users(config_path)["alice"]["skills"] == ["x", "y"]

    def test_edit_nonexistent_fails(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _run(["user", "edit", "nobody", "--role", "admin", "--no-reload"], config_path)
        assert exc.value.code == 1
        assert "does not exist" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# kiso user remove
# ---------------------------------------------------------------------------

class TestIntegrationUserRemove:
    def test_remove_user(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        _run(["user", "remove", "alice", "--no-reload"], config_path)
        assert "alice" not in read_users(config_path)
        assert "removed" in capsys.readouterr().out

    def test_remove_last_admin_fails(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path, users={"boss": {"role": "admin"}})
        with pytest.raises(SystemExit) as exc:
            _run(["user", "remove", "boss", "--no-reload"], config_path)
        assert exc.value.code == 1
        assert "last admin" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# kiso user alias
# ---------------------------------------------------------------------------

class TestIntegrationUserAlias:
    def test_set_alias(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path)
        _run(
            ["user", "alias", "alice", "--connector", "discord", "--id", "alice#1234", "--no-reload"],
            config_path,
        )
        assert read_users(config_path)["alice"]["aliases"]["discord"] == "alice#1234"
        assert "set" in capsys.readouterr().out

    def test_remove_alias(self, tmp_path, capsys):
        config_path = make_user_config(tmp_path, users={
            "boss": {"role": "admin"},
            "alice": {"role": "user", "skills": ["s1"], "aliases": {"discord": "alice#1234"}},
        })
        _run(
            ["user", "alias", "alice", "--connector", "discord", "--remove", "--no-reload"],
            config_path,
        )
        assert "aliases" not in read_users(config_path)["alice"]
        assert "removed" in capsys.readouterr().out
