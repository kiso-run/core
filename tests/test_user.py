"""Tests for cli/user.py — user management CLI commands."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from cli.user import _system_user_exists, _user_add, _read_raw
from tests._cli_user_helpers import make_user_config, read_users


# ── _system_user_exists ──────────────────────────────────────


def test_system_user_exists():
    """pwd.getpwnam succeeds → user exists."""
    with patch("cli.user.pwd.getpwnam") as mock_pw:
        assert _system_user_exists("testuser") is True
        mock_pw.assert_called_once_with("testuser")


def test_system_user_not_exists():
    """pwd.getpwnam raises KeyError → user doesn't exist."""
    with patch("cli.user.pwd.getpwnam", side_effect=KeyError("testuser")):
        assert _system_user_exists("nouser") is False


# ── _user_add with system user warning ───────────────────────


@pytest.fixture()
def user_add_env(tmp_path):
    """Provide a temporary config.toml with one admin user."""
    config = make_user_config(tmp_path, users={"admin1": {"role": "admin"}})
    with patch("cli.user.CONFIG_PATH_DEFAULT", config), \
         patch("cli.user.require_admin"):
        yield config


def _make_add_args(username: str, role: str = "admin", wrappers: str | None = None):
    return argparse.Namespace(
        username=username,
        role=role,
        wrappers=wrappers,
        alias=[],
        no_reload=True,
        user="admin1",
    )


def test_user_add_warns_missing_system_user(user_add_env, capsys):
    """Adding a kiso user whose Linux user doesn't exist shows a warning."""
    with patch("cli.user._system_user_exists", return_value=False):
        _user_add(_make_add_args("newuser"))
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "sudo useradd -m newuser" in err
    assert "won't be functional" in err
    users = read_users(user_add_env)
    assert "newuser" in users


def test_user_add_no_warning_for_existing_system_user(user_add_env, capsys):
    """Adding a kiso user whose Linux user exists shows no warning."""
    with patch("cli.user._system_user_exists", return_value=True):
        _user_add(_make_add_args("newuser"))
    err = capsys.readouterr().err
    assert "does not exist" not in err
    users = read_users(user_add_env)
    assert "newuser" in users
