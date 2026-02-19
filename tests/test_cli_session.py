"""Tests for kiso.cli_session — session listing."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from kiso.cli_session import _relative_time, run_sessions_command


# ── _relative_time ─────────────────────────────────────────


class TestRelativeTime:
    def test_none_returns_unknown(self):
        assert _relative_time(None) == "unknown"

    def test_empty_string_returns_unknown(self):
        assert _relative_time("") == "unknown"

    def test_invalid_string_returns_unknown(self):
        assert _relative_time("not-a-date") == "unknown"

    def test_just_now(self):
        now = datetime.now(timezone.utc)
        assert _relative_time(now.isoformat()) == "just now"

    def test_seconds_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(seconds=30)
        assert _relative_time(dt.isoformat()) == "just now"

    def test_minutes_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _relative_time(dt.isoformat()) == "5m ago"

    def test_hours_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=3)
        assert _relative_time(dt.isoformat()) == "3h ago"

    def test_days_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(days=2)
        assert _relative_time(dt.isoformat()) == "2d ago"

    def test_weeks_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(weeks=3)
        assert _relative_time(dt.isoformat()) == "3w ago"

    def test_naive_datetime_treated_as_utc(self):
        # Naive datetime (no tzinfo) should still work
        dt = datetime.now(timezone.utc) - timedelta(hours=1)
        naive_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        assert _relative_time(naive_str) == "1h ago"

    def test_future_returns_just_now(self):
        dt = datetime.now(timezone.utc) + timedelta(hours=1)
        assert _relative_time(dt.isoformat()) == "just now"


# ── run_sessions_command ──────────────────────────────────


def _make_args(api="http://localhost:8333", show_all=False):
    return argparse.Namespace(api=api, show_all=show_all, command="sessions")


def _mock_config(has_cli_token=True):
    cfg = MagicMock()
    cfg.tokens = {"cli": "tok-abc"} if has_cli_token else {}
    return cfg


class TestRunSessionsCommand:
    def test_missing_cli_token(self, capsys):
        with (
            patch("kiso.config.load_config", return_value=_mock_config(False)),
            pytest.raises(SystemExit, match="1"),
        ):
            run_sessions_command(_make_args())

        out = capsys.readouterr().out
        assert "no 'cli' token" in out

    def test_no_sessions(self, capsys):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.get", return_value=mock_resp),
            patch("getpass.getuser", return_value="alice"),
        ):
            run_sessions_command(_make_args())

        out = capsys.readouterr().out
        assert "No sessions found" in out

    def test_lists_sessions(self, capsys):
        now = datetime.now(timezone.utc)
        two_min_ago = (now - timedelta(minutes=2)).isoformat()
        one_hour_ago = (now - timedelta(hours=1)).isoformat()

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"session": "laptop@marco", "connector": None,
             "description": None, "updated_at": two_min_ago},
            {"session": "dev-backend", "connector": None,
             "description": None, "updated_at": one_hour_ago},
        ]
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.get", return_value=mock_resp),
            patch("getpass.getuser", return_value="marco"),
        ):
            run_sessions_command(_make_args())

        out = capsys.readouterr().out
        assert "laptop@marco" in out
        assert "2m ago" in out
        assert "dev-backend" in out
        assert "1h ago" in out

    def test_lists_sessions_with_connector(self, capsys):
        now = datetime.now(timezone.utc).isoformat()

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"session": "discord_dev", "connector": "discord",
             "description": None, "updated_at": now},
        ]
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.get", return_value=mock_resp),
            patch("getpass.getuser", return_value="admin"),
        ):
            run_sessions_command(_make_args(show_all=True))

        out = capsys.readouterr().out
        assert "discord_dev" in out
        assert "connector: discord" in out

    def test_passes_all_flag(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.get", return_value=mock_resp) as mock_get,
            patch("getpass.getuser", return_value="admin"),
        ):
            run_sessions_command(_make_args(show_all=True))

        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["all"] == "true"

    def test_connection_error(self, capsys):
        with (
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.get", side_effect=httpx.ConnectError("refused")),
            patch("getpass.getuser", return_value="alice"),
            pytest.raises(SystemExit, match="1"),
        ):
            run_sessions_command(_make_args())

        out = capsys.readouterr().out
        assert "cannot connect" in out

    def test_http_error(self, capsys):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with (
            patch("kiso.config.load_config", return_value=_mock_config()),
            patch("httpx.get", side_effect=httpx.HTTPStatusError(
                "err", request=MagicMock(), response=mock_resp)),
            patch("getpass.getuser", return_value="alice"),
            pytest.raises(SystemExit, match="1"),
        ):
            run_sessions_command(_make_args())

        out = capsys.readouterr().out
        assert "403" in out
