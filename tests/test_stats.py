"""Tests for kiso.stats — engine (M54a) and GET /admin/stats endpoint (M54b)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.stats import MODEL_PRICES, _find_price, aggregate, estimate_cost, read_audit_entries
from cli.stats import print_stats, _fmt_k, _fmt_cost

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _entry(**kwargs) -> dict:
    base = {
        "type": "llm",
        "session": "s1",
        "role": "planner",
        "model": "gemini-flash",
        "provider": "openrouter",
        "input_tokens": 100,
        "output_tokens": 50,
        "duration_ms": 500,
        "status": "ok",
        "timestamp": _NOW.isoformat(),
    }
    base.update(kwargs)
    return base


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


# ---------------------------------------------------------------------------
# read_audit_entries — unit tests
# ---------------------------------------------------------------------------


class TestReadAuditEntries:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert read_audit_entries(tmp_path / "noexist") == []

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert read_audit_entries(tmp_path) == []

    def test_filters_non_llm_types(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "test.jsonl", [
            _entry(type="task"),
            _entry(type="review"),
            _entry(type="llm"),
        ])
        result = read_audit_entries(tmp_path)
        assert len(result) == 1
        assert result[0]["type"] == "llm"

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "test.jsonl"
        p.write_text('not-json\n' + json.dumps(_entry()) + '\n{bad\n')
        result = read_audit_entries(tmp_path)
        assert len(result) == 1

    def test_since_filter_excludes_old_entries(self, tmp_path: Path) -> None:
        old = _entry(timestamp=(_NOW - timedelta(days=20)).isoformat())
        new = _entry(timestamp=_NOW.isoformat())
        _write_jsonl(tmp_path / "test.jsonl", [old, new])
        since = _NOW - timedelta(days=10)
        result = read_audit_entries(tmp_path, since=since)
        assert len(result) == 1
        assert result[0]["timestamp"] == _NOW.isoformat()

    def test_malformed_timestamp_entry_included(self, tmp_path: Path) -> None:
        e = _entry(timestamp="not-a-date")
        _write_jsonl(tmp_path / "test.jsonl", [e])
        since = _NOW - timedelta(days=10)
        result = read_audit_entries(tmp_path, since=since)
        assert len(result) == 1  # included because timestamp is unparseable

    def test_unreadable_file_silently_skipped(self, tmp_path: Path) -> None:
        good = tmp_path / "good.jsonl"
        bad = tmp_path / "bad.jsonl"
        _write_jsonl(good, [_entry()])
        _write_jsonl(bad, [_entry()])
        bad.chmod(0o000)
        try:
            result = read_audit_entries(tmp_path)
            # Good file still readable; result must have at least 1 entry
            assert len(result) >= 1
        finally:
            bad.chmod(0o644)


# ---------------------------------------------------------------------------
# aggregate — unit tests
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_empty_entries(self) -> None:
        assert aggregate([], "model") == []

    def test_groups_by_model(self) -> None:
        entries = [
            _entry(model="gemini-flash", input_tokens=100, output_tokens=50),
            _entry(model="gemini-flash", input_tokens=200, output_tokens=80),
            _entry(model="claude-sonnet", input_tokens=50, output_tokens=20),
        ]
        rows = aggregate(entries, "model")
        keys = [r["key"] for r in rows]
        assert "gemini-flash" in keys
        assert "claude-sonnet" in keys
        gf = next(r for r in rows if r["key"] == "gemini-flash")
        assert gf["calls"] == 2
        assert gf["input_tokens"] == 300
        assert gf["output_tokens"] == 130

    def test_sorted_by_total_tokens_descending(self) -> None:
        entries = [
            _entry(model="small", input_tokens=10, output_tokens=5),
            _entry(model="large", input_tokens=1000, output_tokens=500),
            _entry(model="medium", input_tokens=100, output_tokens=50),
        ]
        rows = aggregate(entries, "model")
        assert [r["key"] for r in rows] == ["large", "medium", "small"]

    def test_errors_counted_separately(self) -> None:
        entries = [
            _entry(status="ok"),
            _entry(status="error"),
            _entry(status="error"),
        ]
        rows = aggregate(entries, "model")
        assert rows[0]["calls"] == 3
        assert rows[0]["errors"] == 2

    def test_groups_by_session(self) -> None:
        entries = [_entry(session="alice"), _entry(session="bob"), _entry(session="alice")]
        rows = aggregate(entries, "session")
        alice = next(r for r in rows if r["key"] == "alice")
        assert alice["calls"] == 2

    def test_groups_by_role(self) -> None:
        entries = [_entry(role="planner"), _entry(role="reviewer"), _entry(role="planner")]
        rows = aggregate(entries, "role")
        planner = next(r for r in rows if r["key"] == "planner")
        assert planner["calls"] == 2

    def test_missing_field_falls_back_to_unknown(self) -> None:
        e = {k: v for k, v in _entry().items() if k != "model"}  # no 'model' key
        rows = aggregate([e], "model")
        assert rows[0]["key"] == "unknown"


# ---------------------------------------------------------------------------
# estimate_cost — unit tests
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_known_model_returns_float(self) -> None:
        row = {"key": "google/gemini-flash", "input_tokens": 1_000_000, "output_tokens": 0}
        cost = estimate_cost(row)
        assert cost is not None
        assert abs(cost - 0.075) < 0.001

    def test_unknown_model_returns_none(self) -> None:
        row = {"key": "some-unknown-model-xyz", "input_tokens": 100, "output_tokens": 50}
        assert estimate_cost(row) is None

    def test_zero_tokens_known_model_returns_zero(self) -> None:
        row = {"key": "gemini-flash", "input_tokens": 0, "output_tokens": 0}
        cost = estimate_cost(row)
        assert cost == 0.0

    def test_combined_in_out_cost(self) -> None:
        # gemini-flash: in=0.075, out=0.30 $/MTok
        row = {"key": "gemini-flash", "input_tokens": 1_000_000, "output_tokens": 1_000_000}
        cost = estimate_cost(row)
        assert cost is not None
        assert abs(cost - (0.075 + 0.30)) < 0.001


# ---------------------------------------------------------------------------
# GET /admin/stats — endpoint tests
# ---------------------------------------------------------------------------

AUTH = {"Authorization": "Bearer test-secret-token"}
AUTH_USER = "testadmin"


@pytest.mark.asyncio
class TestStatsEndpoint:
    async def test_requires_admin(self, client, tmp_path: Path) -> None:
        with patch("kiso.main.KISO_DIR", tmp_path):
            resp = await client.get(
                "/admin/stats",
                params={"user": "testuser"},
                headers=AUTH,
            )
        assert resp.status_code == 403

    async def test_empty_audit_returns_empty_rows(self, client, tmp_path: Path) -> None:
        with patch("kiso.main.KISO_DIR", tmp_path):
            resp = await client.get(
                "/admin/stats",
                params={"user": AUTH_USER},
                headers=AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rows"] == []
        assert data["total"]["calls"] == 0

    async def test_aggregates_by_model(self, client, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        _write_jsonl(audit_dir / "today.jsonl", [
            _entry(model="gemini-flash", input_tokens=100, output_tokens=50),
            _entry(model="gemini-flash", input_tokens=200, output_tokens=80),
            _entry(model="claude-sonnet", input_tokens=50, output_tokens=20),
        ])
        with patch("kiso.main.KISO_DIR", tmp_path):
            resp = await client.get(
                "/admin/stats",
                params={"user": AUTH_USER, "by": "model"},
                headers=AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()
        keys = [r["key"] for r in data["rows"]]
        assert "gemini-flash" in keys
        assert "claude-sonnet" in keys
        assert data["total"]["calls"] == 3

    async def test_session_filter(self, client, tmp_path: Path) -> None:
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        _write_jsonl(audit_dir / "today.jsonl", [
            _entry(session="alice", input_tokens=100),
            _entry(session="bob", input_tokens=200),
        ])
        with patch("kiso.main.KISO_DIR", tmp_path):
            resp = await client.get(
                "/admin/stats",
                params={"user": AUTH_USER, "session": "alice"},
                headers=AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"]["calls"] == 1
        assert data["session_filter"] == "alice"


# ---------------------------------------------------------------------------
# print_stats — unit tests (cli.stats)
# ---------------------------------------------------------------------------


class TestPrintStats:
    def _data(self, rows, total=None, by="model", since=30, session_filter=None):
        if total is None:
            total = {
                "calls": sum(r["calls"] for r in rows),
                "errors": 0,
                "input_tokens": sum(r["input_tokens"] for r in rows),
                "output_tokens": sum(r["output_tokens"] for r in rows),
            }
        return {"by": by, "since_days": since, "rows": rows, "total": total, "session_filter": session_filter}

    def test_empty_rows_prints_no_data(self, capsys) -> None:
        print_stats(self._data([], total={"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}))
        out = capsys.readouterr().out
        assert "(no data)" in out

    def test_all_unknown_models_omits_cost_column(self, capsys) -> None:
        rows = [{"key": "unknown-xyz", "calls": 1, "errors": 0, "input_tokens": 100, "output_tokens": 50}]
        print_stats(self._data(rows))
        out = capsys.readouterr().out
        assert "est. cost" not in out

    def test_known_model_shows_cost_column(self, capsys) -> None:
        rows = [{"key": "gemini-flash", "calls": 1, "errors": 0, "input_tokens": 100, "output_tokens": 50}]
        print_stats(self._data(rows))
        out = capsys.readouterr().out
        assert "est. cost" in out

    def test_single_row_renders_correctly(self, capsys) -> None:
        rows = [{"key": "gemini-flash", "calls": 3, "errors": 0, "input_tokens": 500, "output_tokens": 200}]
        print_stats(self._data(rows))
        out = capsys.readouterr().out
        assert "gemini-flash" in out
        assert "total" in out
        assert "3" in out

    def test_session_filter_shown_in_header(self, capsys) -> None:
        rows = [{"key": "gemini-flash", "calls": 1, "errors": 0, "input_tokens": 100, "output_tokens": 50}]
        print_stats(self._data(rows, session_filter="alice"))
        out = capsys.readouterr().out
        assert "[session: alice]" in out

    def test_mixed_known_unknown_models(self, capsys) -> None:
        rows = [
            {"key": "gemini-flash", "calls": 1, "errors": 0, "input_tokens": 100, "output_tokens": 50},
            {"key": "unknown-model-xyz", "calls": 2, "errors": 0, "input_tokens": 200, "output_tokens": 80},
        ]
        print_stats(self._data(rows))
        out = capsys.readouterr().out
        # Cost column shown (at least one known model)
        assert "est. cost" in out
        # Unknown model shows dash
        assert "—" in out


# ---------------------------------------------------------------------------
# _fmt_k and _fmt_cost — unit tests
# ---------------------------------------------------------------------------


class TestFmtHelpers:
    def test_fmt_k_below_1000(self) -> None:
        assert _fmt_k(0) == "0"
        assert _fmt_k(999) == "999"

    def test_fmt_k_1000(self) -> None:
        assert _fmt_k(1000) == "1 k"

    def test_fmt_k_large(self) -> None:
        assert _fmt_k(1_234_000) == "1 234 k"

    def test_fmt_cost_none(self) -> None:
        assert _fmt_cost(None) == "—"

    def test_fmt_cost_zero(self) -> None:
        assert _fmt_cost(0.0) == "$0.00"

    def test_fmt_cost_small(self) -> None:
        assert _fmt_cost(0.005) == "<$0.01"

    def test_fmt_cost_normal(self) -> None:
        assert _fmt_cost(1.23) == "$1.23"


# ---------------------------------------------------------------------------
# _find_price — ordering / specificity tests (M65c)
# ---------------------------------------------------------------------------


class TestFindPriceOrdering:
    """MODEL_PRICES uses first-match-wins: more specific keys must come first."""

    def test_gemini_2_0_flash_not_matched_as_gemini_flash(self) -> None:
        # "gemini-2.0-flash" must match its own entry, not the generic "gemini-flash"
        price = _find_price("gemini-2.0-flash")
        assert price == MODEL_PRICES["gemini-2.0-flash"]
        assert price != MODEL_PRICES["gemini-flash"]

    def test_gemini_2_5_flash_not_matched_as_gemini_flash(self) -> None:
        price = _find_price("gemini-2.5-flash")
        assert price == MODEL_PRICES["gemini-2.5-flash"]
        assert price != MODEL_PRICES["gemini-flash"]

    def test_gemini_flash_matches_generic(self) -> None:
        # A model name that only contains "gemini-flash" (no version digit) uses generic entry
        price = _find_price("some-provider/gemini-flash-latest")
        assert price == MODEL_PRICES["gemini-flash"]

    def test_gpt_4o_mini_not_matched_as_gpt_4o(self) -> None:
        price = _find_price("gpt-4o-mini")
        assert price == MODEL_PRICES["gpt-4o-mini"]
        assert price != MODEL_PRICES["gpt-4o"]

    def test_gpt_4o_not_matched_as_gpt_4(self) -> None:
        price = _find_price("gpt-4o")
        assert price == MODEL_PRICES["gpt-4o"]
        assert price != MODEL_PRICES["gpt-4"]

    def test_gemini_2_5_pro_not_matched_as_gemini_pro(self) -> None:
        price = _find_price("gemini-2.5-pro")
        assert price == MODEL_PRICES["gemini-2.5-pro"]
        assert price != MODEL_PRICES["gemini-pro"]

    def test_llama_3_3_not_matched_as_llama_3(self) -> None:
        price = _find_price("llama-3.3-70b")
        assert price == MODEL_PRICES["llama-3.3"]
        assert price != MODEL_PRICES["llama-3"]

    def test_model_prices_dict_order_preserved(self) -> None:
        """dict preserves insertion order in Python ≥3.7; verify specific before generic."""
        keys = list(MODEL_PRICES.keys())
        # gemini-2.0-flash must appear before gemini-flash
        assert keys.index("gemini-2.0-flash") < keys.index("gemini-flash")
        # gpt-4o-mini must appear before gpt-4o
        assert keys.index("gpt-4o-mini") < keys.index("gpt-4o")
        # gpt-4o must appear before gpt-4
        assert keys.index("gpt-4o") < keys.index("gpt-4")
        # llama-3.3 must appear before llama-3
        assert keys.index("llama-3.3") < keys.index("llama-3")
