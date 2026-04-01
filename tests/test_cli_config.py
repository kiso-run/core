"""Tests for cli/config_cmd.py — kiso config set/get/list."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomli_w


@pytest.fixture()
def cfg_path(tmp_path: Path):
    """Create a minimal config.toml and patch CONFIG_PATH_DEFAULT."""
    p = tmp_path / "config.toml"
    raw = {"settings": {"bot_name": "TestBot", "context_messages": 5, "port": 8333}}
    with open(p, "wb") as f:
        tomli_w.dump(raw, f)
    with patch("cli.config_cmd.CONFIG_PATH_DEFAULT", p):
        yield p


@pytest.fixture()
def _no_admin():
    with patch("cli.config_cmd.require_admin"):
        yield


@pytest.fixture()
def _mock_reload():
    with patch("cli.config_cmd._call_reload") as m:
        yield m


def _make_args(**kw):
    ns = argparse.Namespace(host="localhost", port=8333, user="admin")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# -- config_set ---------------------------------------------------------------

class TestConfigSet:
    def test_set_string(self, cfg_path, _no_admin, _mock_reload, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="bot_name", value="NewBot", config_cmd="set")
        config_set(args)

        out = capsys.readouterr().out
        assert "bot_name = NewBot" in out

        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["settings"]["bot_name"] == "NewBot"

    def test_set_int(self, cfg_path, _no_admin, _mock_reload, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="context_messages", value="10", config_cmd="set")
        config_set(args)

        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["settings"]["context_messages"] == 10

    def test_set_float(self, cfg_path, _no_admin, _mock_reload, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="fact_decay_rate", value="0.25", config_cmd="set")
        config_set(args)

        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["settings"]["fact_decay_rate"] == 0.25

    def test_set_bool_true(self, cfg_path, _no_admin, _mock_reload, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="briefer_enabled", value="true", config_cmd="set")
        config_set(args)

        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["settings"]["briefer_enabled"] is True

    def test_set_bool_false(self, cfg_path, _no_admin, _mock_reload, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="briefer_enabled", value="false", config_cmd="set")
        config_set(args)

        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["settings"]["briefer_enabled"] is False

    def test_set_unknown_key(self, cfg_path, _no_admin, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="nonexistent_key", value="val", config_cmd="set")
        with pytest.raises(SystemExit):
            config_set(args)

        err = capsys.readouterr().err
        assert "unknown setting" in err

    def test_set_invalid_int(self, cfg_path, _no_admin, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="context_messages", value="abc", config_cmd="set")
        with pytest.raises(SystemExit):
            config_set(args)

        err = capsys.readouterr().err
        assert "cannot convert" in err

    def test_set_calls_reload(self, cfg_path, _no_admin, _mock_reload):
        from cli.config_cmd import config_set

        args = _make_args(key="bot_name", value="X", config_cmd="set")
        config_set(args)

        assert _mock_reload.called

    def test_set_list_value(self, cfg_path, _no_admin, _mock_reload, capsys):
        from cli.config_cmd import config_set

        args = _make_args(key="webhook_allow_list", value="http://a,http://b", config_cmd="set")
        config_set(args)

        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["settings"]["webhook_allow_list"] == ["http://a", "http://b"]


# -- config_get ---------------------------------------------------------------

class TestConfigGet:
    def test_get_existing(self, cfg_path, capsys):
        from cli.config_cmd import config_get

        args = _make_args(key="bot_name", config_cmd="get")
        config_get(args)

        out = capsys.readouterr().out
        assert "bot_name = TestBot" in out

    def test_get_default(self, cfg_path, capsys):
        from cli.config_cmd import config_get

        # Key not in config.toml but has a default
        args = _make_args(key="max_replan_depth", config_cmd="get")
        config_get(args)

        out = capsys.readouterr().out
        assert "max_replan_depth = 5" in out

    def test_get_unknown_key(self, cfg_path, capsys):
        from cli.config_cmd import config_get

        args = _make_args(key="no_such_key", config_cmd="get")
        with pytest.raises(SystemExit):
            config_get(args)

        err = capsys.readouterr().err
        assert "unknown setting" in err


# -- config_list ---------------------------------------------------------------

class TestConfigList:
    def test_list_shows_all_keys(self, cfg_path, capsys):
        from cli.config_cmd import config_list
        from kiso.config import SETTINGS_DEFAULTS

        args = _make_args(config_cmd="list")
        config_list(args)

        out = capsys.readouterr().out
        for key in SETTINGS_DEFAULTS:
            assert key in out

    def test_list_marks_overridden(self, cfg_path, capsys):
        from cli.config_cmd import config_list

        args = _make_args(config_cmd="list")
        config_list(args)

        out = capsys.readouterr().out
        # bot_name is in config.toml settings → should have a *
        assert "bot_name = TestBot *" in out
