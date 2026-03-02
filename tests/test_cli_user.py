"""Tests for the kiso user CLI subcommand."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._cli_user_helpers import make_user_config as _make_config
from tests._cli_user_helpers import read_users as _read_users


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _args(**kwargs) -> argparse.Namespace:
    defaults = {"api": "http://localhost:8333"}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestUserList:
    def test_shows_all_users(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_list
            _user_list(_args())

        out = capsys.readouterr().out
        assert "boss" in out
        assert "admin" in out
        assert "alice" in out
        assert "skill1" in out

    def test_shows_aliases(self, tmp_path, capsys):
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin", "aliases": {"discord": "boss#1234"}},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_list
            _user_list(_args())

        out = capsys.readouterr().out
        assert "discord:boss#1234" in out

    def test_empty_config_prints_no_users(self, tmp_path, capsys):
        config_path = _make_config(tmp_path, users={})
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_list
            _user_list(_args())

        assert "No users configured" in capsys.readouterr().out

    def test_wildcard_skills_shown(self, tmp_path, capsys):
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "bob": {"role": "user", "skills": "*"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_list
            _user_list(_args())

        out = capsys.readouterr().out
        assert "skills:  *" in out

    def test_json_output(self, tmp_path, capsys):
        """--json prints valid JSON with all user data."""
        import json as _json
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_list
            _user_list(_args(json=True))

        data = _json.loads(capsys.readouterr().out)
        assert "boss" in data
        assert data["boss"]["role"] == "admin"
        assert "alice" in data
        assert data["alice"]["skills"] == ["skill1", "skill2"]

    def test_json_empty(self, tmp_path, capsys):
        """--json on empty config prints '{}'."""
        import json as _json
        config_path = _make_config(tmp_path, users={})
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_list
            _user_list(_args(json=True))

        data = _json.loads(capsys.readouterr().out)
        assert data == {}


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

class TestUserAdd:
    def test_add_admin(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            _user_add(_args(username="newadmin", role="admin", skills=None, alias=None))

        users = _read_users(config_path)
        assert "newadmin" in users
        assert users["newadmin"]["role"] == "admin"
        assert "skills" not in users["newadmin"]
        assert "User 'newadmin' added" in capsys.readouterr().out

    def test_add_user_with_skill_list(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            _user_add(_args(username="bob", role="user", skills="read,write", alias=None))

        users = _read_users(config_path)
        assert users["bob"]["skills"] == ["read", "write"]

    def test_add_user_with_wildcard_skills(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            _user_add(_args(username="bob", role="user", skills="*", alias=None))

        assert _read_users(config_path)["bob"]["skills"] == "*"

    def test_add_with_alias(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            _user_add(_args(
                username="bob", role="user", skills="*",
                alias=["discord:bob#5678"],
            ))

        users = _read_users(config_path)
        assert users["bob"]["aliases"]["discord"] == "bob#5678"

    def test_add_invalid_username(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit) as exc:
                _user_add(_args(username="INVALID USER!", role="admin", skills=None, alias=None))

        assert exc.value.code == 1
        assert "invalid username" in capsys.readouterr().out

    def test_add_missing_role_rejected(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit) as exc:
                _user_add(_args(username="bob", role=None, skills=None, alias=None))

        assert exc.value.code == 1
        assert "--role" in capsys.readouterr().out

    def test_add_existing_user_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit) as exc:
                _user_add(_args(username="boss", role="admin", skills=None, alias=None))

        assert exc.value.code == 1
        assert "already exists" in capsys.readouterr().out

    def test_add_user_without_skills_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit) as exc:
                _user_add(_args(username="bob", role="user", skills=None, alias=None))

        assert exc.value.code == 1
        assert "--skills" in capsys.readouterr().out

    def test_add_bad_alias_format_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit) as exc:
                _user_add(_args(
                    username="bob", role="user", skills="*",
                    alias=["no-colon-here"],
                ))

        assert exc.value.code == 1
        assert "format" in capsys.readouterr().out

    def test_add_empty_skills_segments_fails(self, tmp_path, capsys):
        """Skills like ',' or 'a,,b' produce empty segments and must be rejected."""
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit) as exc:
                _user_add(_args(username="bob", role="user", skills=",", alias=None))

        assert exc.value.code == 1
        assert "no valid skill" in capsys.readouterr().out

    def test_add_strips_whitespace_from_skills(self, tmp_path):
        """Skills like 'a , b' are normalized to ['a', 'b']."""
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_add
            _user_add(_args(username="bob", role="user", skills=" read , write ", alias=None))

        assert _read_users(config_path)["bob"]["skills"] == ["read", "write"]

    def test_add_no_reload_skips_reload(self, tmp_path):
        """--no-reload writes config but does not call _call_reload."""
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload") as mock_reload,
        ):
            from cli.user import _user_add
            _user_add(_args(username="bob", role="admin", skills=None, alias=None, no_reload=True))

        mock_reload.assert_not_called()
        assert "bob" in _read_users(config_path)


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

class TestUserEdit:
    def test_edit_role_to_admin(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            _user_edit(_args(username="alice", role="admin", skills=None))

        users = _read_users(config_path)
        assert users["alice"]["role"] == "admin"
        assert "updated" in capsys.readouterr().out

    def test_edit_skills(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            _user_edit(_args(username="alice", role=None, skills="read,write"))

        assert _read_users(config_path)["alice"]["skills"] == ["read", "write"]

    def test_edit_no_args_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            with pytest.raises(SystemExit) as exc:
                _user_edit(_args(username="alice", role=None, skills=None))

        assert exc.value.code == 1
        assert "at least one" in capsys.readouterr().out

    def test_edit_nonexistent_user_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            with pytest.raises(SystemExit) as exc:
                _user_edit(_args(username="nobody", role="admin", skills=None))

        assert exc.value.code == 1
        assert "does not exist" in capsys.readouterr().out

    def test_edit_demote_last_admin_fails(self, tmp_path, capsys):
        """Cannot demote the only admin to user."""
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "alice": {"role": "user", "skills": "*"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            with pytest.raises(SystemExit) as exc:
                _user_edit(_args(username="boss", role="user", skills="*"))

        assert exc.value.code == 1
        assert "last admin" in capsys.readouterr().out

    def test_edit_demote_admin_when_another_exists(self, tmp_path):
        """Demoting admin when another admin exists is allowed."""
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "boss2": {"role": "admin"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            _user_edit(_args(username="boss", role="user", skills="read"))

        assert _read_users(config_path)["boss"]["role"] == "user"

    def test_edit_role_user_without_skills_fails(self, tmp_path, capsys):
        """Setting role=user on a user with no skills and no --skills fails."""
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "bob": {"role": "admin"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            with pytest.raises(SystemExit) as exc:
                _user_edit(_args(username="bob", role="user", skills=None))

        assert exc.value.code == 1
        assert "--skills" in capsys.readouterr().out

    def test_edit_wildcard_skills(self, tmp_path):
        """--skills '*' is stored as the literal string '*', not a list."""
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            _user_edit(_args(username="alice", role=None, skills="*"))

        assert _read_users(config_path)["alice"]["skills"] == "*"

    def test_edit_promote_to_admin_preserves_skills(self, tmp_path):
        """Promoting a user to admin leaves the skills key intact."""
        config_path = _make_config(tmp_path)  # alice has skills: ["skill1", "skill2"]
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_edit
            _user_edit(_args(username="alice", role="admin", skills=None))

        alice = _read_users(config_path)["alice"]
        assert alice["role"] == "admin"
        assert alice["skills"] == ["skill1", "skill2"]


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

class TestUserRemove:
    def test_remove_user(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_remove
            _user_remove(_args(username="alice"))

        users = _read_users(config_path)
        assert "alice" not in users
        assert "User 'alice' removed" in capsys.readouterr().out

    def test_remove_nonexistent_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_remove
            with pytest.raises(SystemExit) as exc:
                _user_remove(_args(username="nobody"))

        assert exc.value.code == 1
        assert "does not exist" in capsys.readouterr().out

    def test_remove_last_admin_fails(self, tmp_path, capsys):
        """Removing the only admin is rejected to prevent lockout."""
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "alice": {"role": "user", "skills": "*"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_remove
            with pytest.raises(SystemExit) as exc:
                _user_remove(_args(username="boss"))

        assert exc.value.code == 1
        assert "last admin" in capsys.readouterr().out

    def test_remove_non_last_admin_ok(self, tmp_path):
        """Removing an admin when another admin exists is allowed."""
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "boss2": {"role": "admin"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_remove
            _user_remove(_args(username="boss"))

        users = _read_users(config_path)
        assert "boss" not in users
        assert "boss2" in users


# ---------------------------------------------------------------------------
# alias
# ---------------------------------------------------------------------------

class TestUserAlias:
    def test_add_alias(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_alias
            _user_alias(_args(
                username="alice", connector="discord", id="alice#9999", remove=False,
            ))

        users = _read_users(config_path)
        assert users["alice"]["aliases"]["discord"] == "alice#9999"
        assert "set" in capsys.readouterr().out

    def test_update_alias(self, tmp_path):
        """Adding an alias for a connector that already exists updates it."""
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "alice": {
                "role": "user", "skills": ["skill1"],
                "aliases": {"discord": "alice#old"},
            },
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_alias
            _user_alias(_args(
                username="alice", connector="discord", id="alice#new", remove=False,
            ))

        assert _read_users(config_path)["alice"]["aliases"]["discord"] == "alice#new"

    def test_remove_alias(self, tmp_path, capsys):
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "alice": {
                "role": "user", "skills": ["skill1"],
                "aliases": {"discord": "alice#1234"},
            },
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_alias
            _user_alias(_args(
                username="alice", connector="discord", id=None, remove=True,
            ))

        users = _read_users(config_path)
        assert "aliases" not in users["alice"]
        assert "removed" in capsys.readouterr().out

    def test_remove_nonexistent_alias_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_alias
            with pytest.raises(SystemExit) as exc:
                _user_alias(_args(
                    username="alice", connector="discord", id=None, remove=True,
                ))

        assert exc.value.code == 1
        assert "no alias" in capsys.readouterr().out

    def test_alias_nonexistent_user_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_alias
            with pytest.raises(SystemExit) as exc:
                _user_alias(_args(
                    username="nobody", connector="discord", id="x#1", remove=False,
                ))

        assert exc.value.code == 1
        assert "does not exist" in capsys.readouterr().out

    def test_alias_missing_id_fails(self, tmp_path, capsys):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_alias
            with pytest.raises(SystemExit) as exc:
                _user_alias(_args(
                    username="alice", connector="discord", id=None, remove=False,
                ))

        assert exc.value.code == 1
        assert "--id" in capsys.readouterr().out

    def test_alias_invalid_connector_name_fails(self, tmp_path, capsys):
        """Connector names with special chars are rejected before writing config."""
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._call_reload"),
        ):
            from cli.user import _user_alias
            with pytest.raises(SystemExit) as exc:
                _user_alias(_args(
                    username="alice", connector="bad connector!", id="x#1", remove=False,
                ))

        assert exc.value.code == 1
        assert "invalid connector" in capsys.readouterr().out


class TestRunUserCommandDispatch:
    def test_no_subcommand_prints_usage(self, capsys):
        with pytest.raises(SystemExit) as exc:
            from cli.user import run_user_command
            run_user_command(_args(user_command=None))

        assert exc.value.code == 1
        assert "usage" in capsys.readouterr().out


# --- M84h: _read_raw FileNotFoundError ---


class TestReadRaw:
    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        """M84h: _read_raw must return {} when config.toml does not exist."""
        from cli.user import _read_raw
        result = _read_raw(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_returns_parsed_content_when_file_exists(self, tmp_path):
        """_read_raw returns parsed TOML when the file is present."""
        from cli.user import _read_raw
        p = tmp_path / "config.toml"
        p.write_bytes(b'[tokens]\ncli = "tok"\n')
        result = _read_raw(p)
        assert result["tokens"]["cli"] == "tok"
