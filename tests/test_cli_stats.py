"""Tests for cli/stats.py — token usage stats command."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cli.stats import _fmt_cost, _fmt_k, print_stats, run_stats_command


# ---------------------------------------------------------------------------
# _fmt_k
# ---------------------------------------------------------------------------

class TestFmtK:
    def test_below_1000(self):
        assert _fmt_k(0) == "0"
        assert _fmt_k(999) == "999"

    def test_exact_1000(self):
        assert _fmt_k(1000) == "1 k"

    def test_thousands(self):
        assert _fmt_k(1234) == "1 k"       # truncated, not rounded
        assert _fmt_k(1999) == "1 k"

    def test_large(self):
        assert _fmt_k(1_234_000) == "1 234 k"


# ---------------------------------------------------------------------------
# _fmt_cost
# ---------------------------------------------------------------------------

class TestFmtCost:
    def test_none(self):
        assert _fmt_cost(None) == "—"

    def test_zero(self):
        assert _fmt_cost(0.0) == "$0.00"

    def test_below_cent(self):
        assert _fmt_cost(0.001) == "<$0.01"

    def test_normal(self):
        assert _fmt_cost(1.23) == "$1.23"

    def test_large(self):
        assert _fmt_cost(12.345) == "$12.35"


# ---------------------------------------------------------------------------
# print_stats
# ---------------------------------------------------------------------------

_ONE_ROW = {
    "by": "model",
    "since_days": 30,
    "rows": [{"key": "gpt-4", "calls": 10, "input_tokens": 5000, "output_tokens": 1000}],
    "total": {"calls": 10, "input_tokens": 5000, "output_tokens": 1000},
}

_NO_ROWS = {
    "by": "model",
    "since_days": 7,
    "rows": [],
    "total": {},
}


class TestPrintStats:
    def test_header_by_model(self, capsys):
        print_stats(_ONE_ROW)
        out = capsys.readouterr().out
        assert "Token usage — last 30 days  (by model)" in out

    def test_header_with_session_filter(self, capsys):
        data = {**_ONE_ROW, "session_filter": "alice"}
        print_stats(data)
        out = capsys.readouterr().out
        assert "[session: alice]" in out

    def test_no_rows_prints_no_data(self, capsys):
        print_stats(_NO_ROWS)
        assert "(no data)" in capsys.readouterr().out

    def test_row_key_and_calls_appear(self, capsys):
        print_stats(_ONE_ROW)
        out = capsys.readouterr().out
        assert "gpt-4" in out
        assert "10" in out

    def test_total_line_appears(self, capsys):
        print_stats(_ONE_ROW)
        out = capsys.readouterr().out
        assert "total" in out

    def test_multiple_rows(self, capsys):
        data = {
            "by": "session",
            "since_days": 30,
            "rows": [
                {"key": "alice", "calls": 5, "input_tokens": 1000, "output_tokens": 200},
                {"key": "bob",   "calls": 3, "input_tokens": 800,  "output_tokens": 100},
            ],
            "total": {"calls": 8, "input_tokens": 1800, "output_tokens": 300},
        }
        print_stats(data)
        out = capsys.readouterr().out
        assert "alice" in out
        assert "bob" in out
        assert "total" in out

    def test_cost_column_shown_when_known(self, capsys):
        with patch("kiso.stats.estimate_cost", return_value=1.23):
            print_stats(_ONE_ROW)
        out = capsys.readouterr().out
        assert "est. cost" in out
        assert "$1.23" in out

    def test_cost_column_hidden_when_unknown(self, capsys):
        with patch("kiso.stats.estimate_cost", return_value=None):
            print_stats(_ONE_ROW)
        out = capsys.readouterr().out
        assert "est. cost" not in out


# ---------------------------------------------------------------------------
# run_stats_command
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    ns = {"api": "http://localhost:8333", "since": 30, "session": None, "by": "model"}
    ns.update(kwargs)
    return argparse.Namespace(**ns)


class TestRunStatsCommand:
    def _mock_cfg(self, token="test-token"):
        cfg = MagicMock()
        cfg.tokens.get.return_value = token
        return cfg

    def test_success_calls_print_stats(self, capsys):
        payload = {**_ONE_ROW}
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload

        with (
            patch("kiso.config.load_config", return_value=self._mock_cfg()),
            patch("httpx.get", return_value=mock_resp),
            patch("cli.stats.print_stats") as mock_print,
        ):
            run_stats_command(_make_args())

        mock_print.assert_called_once_with(payload)

    def test_no_cli_token_exits(self, capsys):
        with (
            patch("kiso.config.load_config", return_value=self._mock_cfg(token=None)),
        ):
            with pytest.raises(SystemExit) as exc:
                run_stats_command(_make_args())

        assert exc.value.code == 1
        assert "no 'cli' token" in capsys.readouterr().out

    def test_connect_error_exits(self, capsys):
        with (
            patch("kiso.config.load_config", return_value=self._mock_cfg()),
            patch("httpx.get", side_effect=httpx.ConnectError("refused")),
        ):
            with pytest.raises(SystemExit) as exc:
                run_stats_command(_make_args())

        assert exc.value.code == 1
        assert "cannot connect" in capsys.readouterr().out

    def test_http_error_exits(self, capsys):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with (
            patch("kiso.config.load_config", return_value=self._mock_cfg()),
            patch("httpx.get", side_effect=httpx.HTTPStatusError(
                "403", request=MagicMock(), response=mock_resp,
            )),
        ):
            with pytest.raises(SystemExit) as exc:
                run_stats_command(_make_args())

        assert exc.value.code == 1
        assert "403" in capsys.readouterr().out

    def test_session_filter_included_in_params(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _NO_ROWS

        with (
            patch("kiso.config.load_config", return_value=self._mock_cfg()),
            patch("httpx.get", return_value=mock_resp) as mock_get,
            patch("cli.stats.print_stats"),
        ):
            run_stats_command(_make_args(session="alice"))

        call_kwargs = mock_get.call_args
        assert call_kwargs.kwargs["params"]["session"] == "alice"

    def test_no_session_filter_omitted_from_params(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _NO_ROWS

        with (
            patch("kiso.config.load_config", return_value=self._mock_cfg()),
            patch("httpx.get", return_value=mock_resp) as mock_get,
            patch("cli.stats.print_stats"),
        ):
            run_stats_command(_make_args(session=None))

        call_kwargs = mock_get.call_args
        assert "session" not in call_kwargs.kwargs["params"]
