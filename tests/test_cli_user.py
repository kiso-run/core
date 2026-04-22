"""Tests for the ``kiso user`` CLI subcommand.

Covers list / add / edit / remove with the v0.10 allowlist fields:
``mcp`` and ``skills`` (comma-separated or ``*``). The retired
``wrappers`` field and its CLI flag are gone.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from tests._cli_user_helpers import make_user_config as _make_config
from tests._cli_user_helpers import read_users as _read_users


def _args(**kwargs) -> argparse.Namespace:
    defaults = {"api": "http://localhost:8333", "no_reload": True, "mcp": None, "skills": None}
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

    def test_wildcard_mcp_shown(self, tmp_path, capsys):
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
            "bob": {"role": "user", "mcp": "*"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_list
            _user_list(_args())

        out = capsys.readouterr().out
        assert "mcp:      *" in out


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestUserAdd:
    def test_add_admin(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            _user_add(_args(username="newadmin", role="admin", alias=None))

        users = _read_users(config_path)
        assert users["newadmin"] == {"role": "admin"}

    def test_add_user_with_skills_list(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            _user_add(_args(
                username="bob", role="user",
                skills="python-debug,writing-style", alias=None,
            ))

        users = _read_users(config_path)
        assert users["bob"]["role"] == "user"
        assert users["bob"]["skills"] == ["python-debug", "writing-style"]
        assert "mcp" not in users["bob"]

    def test_add_user_with_mcp_star(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            _user_add(_args(
                username="bob", role="user", mcp="*", alias=None,
            ))

        users = _read_users(config_path)
        assert users["bob"]["mcp"] == "*"

    def test_add_user_with_mcp_and_skills(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            _user_add(_args(
                username="bob", role="user",
                mcp="search:web,filesystem:list",
                skills="python-debug",
                alias=None,
            ))

        users = _read_users(config_path)
        assert users["bob"]["mcp"] == ["search:web", "filesystem:list"]
        assert users["bob"]["skills"] == ["python-debug"]

    def test_add_with_alias(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            _user_add(_args(
                username="bob", role="user", mcp="*",
                alias=["discord:bob123"],
            ))

        users = _read_users(config_path)
        assert users["bob"]["aliases"] == {"discord": "bob123"}

    def test_add_rejects_invalid_name(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit):
                _user_add(_args(username="INVALID USER!", role="admin", alias=None))

    def test_add_duplicate_rejected(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit):
                _user_add(_args(username="boss", role="admin", alias=None))

    def test_add_rejects_empty_allowlist(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
            patch("cli.user._system_user_exists", return_value=True),
        ):
            from cli.user import _user_add
            with pytest.raises(SystemExit):
                _user_add(_args(
                    username="bob", role="user", skills=",", alias=None,
                ))


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


class TestUserEdit:
    def test_edit_role(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_edit
            _user_edit(_args(username="alice", role="admin"))

        users = _read_users(config_path)
        assert users["alice"]["role"] == "admin"

    def test_edit_skills(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_edit
            _user_edit(_args(username="alice", role=None, skills="new-skill"))

        users = _read_users(config_path)
        assert users["alice"]["skills"] == ["new-skill"]

    def test_edit_requires_at_least_one_flag(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_edit
            with pytest.raises(SystemExit):
                _user_edit(_args(username="alice", role=None))

    def test_edit_unknown_user_rejected(self, tmp_path):
        config_path = _make_config(tmp_path)
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_edit
            with pytest.raises(SystemExit):
                _user_edit(_args(username="nobody", role="admin"))

    def test_cannot_demote_last_admin(self, tmp_path):
        config_path = _make_config(tmp_path, users={
            "boss": {"role": "admin"},
        })
        with (
            patch("cli.user.require_admin"),
            patch("cli.user.CONFIG_PATH_DEFAULT", config_path),
        ):
            from cli.user import _user_edit
            with pytest.raises(SystemExit):
                _user_edit(_args(username="boss", role="user", mcp="*"))
