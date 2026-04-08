"""CLI tests for cron job management commands (cli/cron.py)."""

from __future__ import annotations

import sys
from unittest.mock import patch, MagicMock

import pytest

from tests._cli_test_helpers import make_cli_args, mock_cli_config, mock_http_response


# ── cron_list ─────────────────────────────────────────────────


class TestCronList:
    def test_empty(self, capsys):
        from cli.cron import cron_list

        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"jobs": []}):
            cron_list(args)
        assert "No cron jobs configured" in capsys.readouterr().out

    def test_multiple_jobs(self, capsys):
        from cli.cron import cron_list

        jobs = [
            {"id": 1, "enabled": True, "schedule": "0 9 * * *",
             "session": "daily", "prompt": "Run backup", "next_run": "2026-04-08 09:00"},
            {"id": 2, "enabled": False, "schedule": "*/5 * * * *",
             "session": "monitor", "prompt": "Check health", "next_run": "?"},
        ]
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"jobs": jobs}):
            cron_list(args)
        out = capsys.readouterr().out
        assert "[1]" in out
        assert "[ON]" in out
        assert "0 9 * * *" in out
        assert "daily" in out
        assert "[2]" in out
        assert "[OFF]" in out
        assert "Check health" in out

    def test_prompt_truncation(self, capsys):
        from cli.cron import cron_list

        long_prompt = "A" * 80
        jobs = [{"id": 1, "enabled": True, "schedule": "0 * * * *",
                 "session": "s", "prompt": long_prompt, "next_run": "?"}]
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"jobs": jobs}):
            cron_list(args)
        out = capsys.readouterr().out
        assert "..." in out
        assert long_prompt not in out

    def test_session_filter(self):
        from cli.cron import cron_list

        args = make_cli_args(session="my-session")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"jobs": []}) as mock_req:
            cron_list(args)
        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs["params"]["session"] == "my-session"

    def test_no_session_filter(self):
        from cli.cron import cron_list

        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"jobs": []}) as mock_req:
            cron_list(args)
        params = mock_req.call_args.kwargs.get("params") or {}
        assert "session" not in params


# ── cron_add ──────────────────────────────────────────────────


class TestCronAdd:
    def test_happy_path(self, capsys):
        from cli.cron import cron_add

        mock_croniter = MagicMock()
        mock_croniter.is_valid.return_value = True
        args = make_cli_args(session="daily", schedule="0 9 * * *", prompt="Run backup")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             patch.dict("sys.modules", {"croniter": MagicMock(croniter=mock_croniter)}), \
             mock_http_response({"id": 5, "schedule": "0 9 * * *",
                                 "session": "daily", "next_run": "2026-04-09 09:00"}):
            cron_add(args)
        out = capsys.readouterr().out
        assert "id=5" in out
        assert "0 9 * * *" in out
        assert "daily" in out

    def test_invalid_cron_expression(self, capsys):
        from cli.cron import cron_add

        mock_croniter = MagicMock()
        mock_croniter.is_valid.return_value = False
        args = make_cli_args(session="s", schedule="not valid", prompt="x")
        with patch("cli.plugin_ops.require_admin"), \
             patch.dict("sys.modules", {"croniter": MagicMock(croniter=mock_croniter)}), \
             pytest.raises(SystemExit):
            cron_add(args)
        err = capsys.readouterr().err
        assert "invalid cron expression" in err

    def test_croniter_import_error(self, capsys):
        from cli.cron import cron_add

        args = make_cli_args(session="s", schedule="0 9 * * *", prompt="x")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             patch.dict("sys.modules", {"croniter": None}), \
             mock_http_response({"id": 1, "schedule": "0 9 * * *",
                                 "session": "s", "next_run": "?"}):
            # croniter import fails — should fall through to server validation
            cron_add(args)
        out = capsys.readouterr().out
        assert "id=1" in out

    def test_require_admin(self):
        from cli.cron import cron_add

        args = make_cli_args(session="s", schedule="0 * * * *", prompt="x")
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            cron_add(args)


# ── cron_remove ───────────────────────────────────────────────


class TestCronRemove:
    def test_deleted(self, capsys):
        from cli.cron import cron_remove

        args = make_cli_args(job_id=7)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"deleted": True}):
            cron_remove(args)
        assert "7 removed" in capsys.readouterr().out

    def test_not_deleted(self, capsys):
        from cli.cron import cron_remove

        args = make_cli_args(job_id=99)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"deleted": False}), \
             pytest.raises(SystemExit):
            cron_remove(args)
        assert "could not remove" in capsys.readouterr().err

    def test_require_admin(self):
        from cli.cron import cron_remove

        args = make_cli_args(job_id=1)
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            cron_remove(args)


# ── cron_enable / cron_disable ────────────────────────────────


class TestCronEnable:
    def test_enable(self, capsys):
        from cli.cron import cron_enable

        args = make_cli_args(job_id=3)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({}):
            cron_enable(args)
        assert "3 enabled" in capsys.readouterr().out

    def test_require_admin(self):
        from cli.cron import cron_enable

        args = make_cli_args(job_id=3)
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            cron_enable(args)


class TestCronDisable:
    def test_disable(self, capsys):
        from cli.cron import cron_disable

        args = make_cli_args(job_id=4)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({}):
            cron_disable(args)
        assert "4 disabled" in capsys.readouterr().out

    def test_require_admin(self):
        from cli.cron import cron_disable

        args = make_cli_args(job_id=4)
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            cron_disable(args)
