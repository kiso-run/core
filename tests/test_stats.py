"""Tests for kiso.stats — engine (M54a) and GET /admin/stats endpoint (M54b)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.stats import aggregate, estimate_cost, read_audit_entries

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
