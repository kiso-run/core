"""Tests for kiso.cli_env — deploy secret management."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cli.env import (
    _parse_key,
    _parse_value,
    _read_lines,
    _write_lines,
    run_env_command,
)


# ── Helpers ──────────────────────────────────────────────


def _make_args(env_command=None, **kwargs):
    ns = {"env_command": env_command, "api": "http://localhost:8333", "command": "env"}
    ns.update(kwargs)
    return argparse.Namespace(**ns)


def _mock_admin():
    """Patch _require_admin to be a no-op."""
    return patch("cli.env.require_admin")


# ── _parse_key ──────────────────────────────────────────


class TestParseKey:
    def test_normal_line(self):
        assert _parse_key("FOO=bar") == "FOO"

    def test_spaces_around_key(self):
        assert _parse_key("  FOO = bar") == "FOO"

    def test_comment_line(self):
        assert _parse_key("# comment") is None

    def test_blank_line(self):
        assert _parse_key("") is None
        assert _parse_key("   ") is None

    def test_no_equals(self):
        assert _parse_key("JUST_A_KEY") is None


# ── _parse_value ────────────────────────────────────────


class TestParseValue:
    def test_simple_value(self):
        assert _parse_value("KEY=value123") == "value123"

    def test_double_quoted(self):
        assert _parse_value('KEY="hello world"') == "hello world"

    def test_single_quoted(self):
        assert _parse_value("KEY='hello world'") == "hello world"

    def test_unquoted_with_spaces(self):
        # Stripped but not dequoted — spaces kept
        assert _parse_value("KEY=hello") == "hello"

    def test_empty_value(self):
        assert _parse_value("KEY=") == ""


# ── _read_lines / _write_lines ─────────────────────────


class TestReadWriteLines:
    def test_read_missing_file(self, tmp_path):
        assert _read_lines(tmp_path / ".env") == []

    def test_round_trip(self, tmp_path):
        p = tmp_path / ".env"
        lines = ["# header", "FOO=bar", "BAZ=qux"]
        _write_lines(lines, p)
        assert _read_lines(p) == lines

    def test_write_empty_list(self, tmp_path):
        p = tmp_path / ".env"
        _write_lines([], p)
        assert p.read_text() == ""

    def test_write_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "sub" / "dir" / ".env"
        _write_lines(["A=1"], p)
        assert _read_lines(p) == ["A=1"]


# ── run_env_command dispatch ────────────────────────────


class TestRunEnvCommandDispatch:
    def test_no_subcommand_exits(self, capsys):
        with pytest.raises(SystemExit, match="1"):
            run_env_command(_make_args(env_command=None))
        out = capsys.readouterr().out
        assert "usage:" in out


# ── env set ─────────────────────────────────────────────


class TestEnvSet:
    def test_creates_new_file(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("set", key="API_KEY", value="sk-123"))

        content = env_file.read_text()
        assert "API_KEY=sk-123" in content
        assert "set." in capsys.readouterr().out

    def test_appends_to_existing(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING=val\n")
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("set", key="NEW_KEY", value="new"))

        content = env_file.read_text()
        assert "EXISTING=val" in content
        assert "NEW_KEY=new" in content

    def test_updates_existing_key(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nAPI_KEY=old\nOTHER=keep\n")
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("set", key="API_KEY", value="new"))

        content = env_file.read_text()
        assert "API_KEY=new" in content
        assert "API_KEY=old" not in content
        assert "# comment" in content
        assert "OTHER=keep" in content

    def test_preserves_comments(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("# Deploy secrets\n\nKEY=val\n")
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("set", key="KEY", value="updated"))

        lines = env_file.read_text().splitlines()
        assert lines[0] == "# Deploy secrets"
        assert lines[1] == ""


# ── env get ─────────────────────────────────────────────


class TestEnvGet:
    def test_gets_value(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=sk-abc\n")
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("get", key="API_KEY"))

        assert "sk-abc" in capsys.readouterr().out

    def test_gets_quoted_value(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text('API_KEY="sk-quoted"\n')
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("get", key="API_KEY"))

        assert "sk-quoted" in capsys.readouterr().out

    def test_missing_key_exits(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=val\n")
        with (
            _mock_admin(),
            patch("cli.env.ENV_FILE", env_file),
            pytest.raises(SystemExit, match="1"),
        ):
            run_env_command(_make_args("get", key="MISSING"))

        assert "not found" in capsys.readouterr().out

    def test_empty_file_exits(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        with (
            _mock_admin(),
            patch("cli.env.ENV_FILE", env_file),
            pytest.raises(SystemExit, match="1"),
        ):
            run_env_command(_make_args("get", key="ANY"))


# ── env list ────────────────────────────────────────────


class TestEnvList:
    def test_lists_keys(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("# header\nALPHA=1\nBETA=2\n")
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("list"))

        out = capsys.readouterr().out
        assert "ALPHA" in out
        assert "BETA" in out

    def test_empty_file(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("# only comments\n")
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("list"))

        assert "No deploy secrets" in capsys.readouterr().out

    def test_no_file(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("list"))

        assert "No deploy secrets" in capsys.readouterr().out


# ── env delete ──────────────────────────────────────────


class TestEnvDelete:
    def test_deletes_key(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("# header\nALPHA=1\nBETA=2\n")
        with _mock_admin(), patch("cli.env.ENV_FILE", env_file):
            run_env_command(_make_args("delete", key="ALPHA"))

        content = env_file.read_text()
        assert "ALPHA" not in content
        assert "BETA=2" in content
        assert "# header" in content
        assert "deleted" in capsys.readouterr().out

    def test_missing_key_exits(self, tmp_path, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text("ONLY=this\n")
        with (
            _mock_admin(),
            patch("cli.env.ENV_FILE", env_file),
            pytest.raises(SystemExit, match="1"),
        ):
            run_env_command(_make_args("delete", key="NOPE"))

        assert "not found" in capsys.readouterr().out


# ── env reload ──────────────────────────────────────────


def _mock_config(has_cli_token=True):
    cfg = MagicMock()
    cfg.tokens = {"cli": "tok-abc"} if has_cli_token else {}
    return cfg


class TestEnvReload:
    def test_successful_reload(self, capsys):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"reloaded": True, "keys_loaded": 5}
        mock_resp.raise_for_status = MagicMock()

        with (
            _mock_admin(),
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.post", return_value=mock_resp),
            patch("getpass.getuser", return_value="admin"),
        ):
            run_env_command(_make_args("reload"))

        out = capsys.readouterr().out
        assert "Reloaded" in out
        assert "5 keys" in out

    def test_missing_cli_token(self, capsys):
        with (
            _mock_admin(),
            patch("kiso.config.load_config", return_value=_mock_config(False)),
            pytest.raises(SystemExit, match="1"),
        ):
            run_env_command(_make_args("reload"))

        assert "no 'cli' token" in capsys.readouterr().out

    def test_connection_error(self, capsys):
        with (
            _mock_admin(),
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.post", side_effect=httpx.ConnectError("refused")),
            patch("getpass.getuser", return_value="admin"),
            pytest.raises(SystemExit, match="1"),
        ):
            run_env_command(_make_args("reload"))

        assert "cannot connect" in capsys.readouterr().out

    def test_http_error(self, capsys):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with (
            _mock_admin(),
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.post", side_effect=httpx.HTTPStatusError(
                "err", request=MagicMock(), response=mock_resp)),
            patch("getpass.getuser", return_value="admin"),
            pytest.raises(SystemExit, match="1"),
        ):
            run_env_command(_make_args("reload"))

        assert "403" in capsys.readouterr().out
