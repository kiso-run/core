"""Tests for cli/stats.py — run_stats_command (HTTP glue).

Format helpers (_fmt_k, _fmt_cost) and print_stats are already
covered comprehensively in tests/test_stats.py.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cli.stats import run_stats_command


def _make_args(**kwargs):
    ns = {"api": "http://localhost:8333", "since": 30, "session": None, "by": "model"}
    ns.update(kwargs)
    return argparse.Namespace(**ns)


def _mock_cfg(token="test-token"):
    cfg = MagicMock()
    cfg.tokens = {"cli": token} if token else {}
    return cfg


# ---------------------------------------------------------------------------
# run_stats_command
# ---------------------------------------------------------------------------

class TestRunStatsCommand:
    def test_success_calls_print_stats(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"by": "model", "since_days": 30, "rows": [], "total": {}}

        with (
            patch("kiso.config.load_config", return_value=_mock_cfg()),
            patch("httpx.request", return_value=mock_resp),
            patch("cli.stats.print_stats") as mock_print,
        ):
            run_stats_command(_make_args())

        mock_print.assert_called_once_with(mock_resp.json.return_value)

    def test_no_cli_token_exits(self, capsys):
        with (
            patch("kiso.config.load_config", return_value=_mock_cfg(token=None)),
        ):
            with pytest.raises(SystemExit) as exc:
                run_stats_command(_make_args())

        assert exc.value.code == 1
        assert "no 'cli' token" in capsys.readouterr().err

    def test_connect_error_exits(self, capsys):
        with (
            patch("kiso.config.load_config", return_value=_mock_cfg()),
            patch("httpx.request", side_effect=httpx.ConnectError("refused")),
        ):
            with pytest.raises(SystemExit) as exc:
                run_stats_command(_make_args())

        assert exc.value.code == 1
        assert "cannot connect" in capsys.readouterr().err

    def test_http_error_exits(self, capsys):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with (
            patch("kiso.config.load_config", return_value=_mock_cfg()),
            patch("httpx.request", side_effect=httpx.HTTPStatusError(
                "403", request=MagicMock(), response=mock_resp,
            )),
        ):
            with pytest.raises(SystemExit) as exc:
                run_stats_command(_make_args())

        assert exc.value.code == 1
        assert "403" in capsys.readouterr().err

    def test_session_filter_included_in_params(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}

        with (
            patch("kiso.config.load_config", return_value=_mock_cfg()),
            patch("httpx.request", return_value=mock_resp) as mock_req,
            patch("cli.stats.print_stats"),
        ):
            run_stats_command(_make_args(session="alice"))

        assert mock_req.call_args.kwargs["params"]["session"] == "alice"

    def test_no_session_filter_omitted_from_params(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}

        with (
            patch("kiso.config.load_config", return_value=_mock_cfg()),
            patch("httpx.request", return_value=mock_resp) as mock_req,
            patch("cli.stats.print_stats"),
        ):
            run_stats_command(_make_args(session=None))

        assert "session" not in mock_req.call_args.kwargs["params"]
