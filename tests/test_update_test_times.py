"""Unit tests for ``utils/update_test_times.py``.

The updater parses a ``run_tests.sh`` recap block (the
``━━━ RECAP ━━━`` section printed at the end of every run) and
updates ``utils/test_times.json`` with the observed count and
average seconds-per-test for each tier.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "utils"))

import update_test_times as U  # noqa: E402


# ---------------------------------------------------------------------------
# parse_recap — extract (count, elapsed_s) per tier
# ---------------------------------------------------------------------------


class TestParseRecap:
    def test_parses_pytest_style_line(self):
        """Pytest output style: `4379 passed in 88.90s`."""
        log = """
━━━ RECAP ━━━

  ✓ Unit tests               4379 passed in 88.90s
  ✓ Integration tests        58 passed in 5.51s
"""
        result = U.parse_recap(log)
        assert result["Unit tests"] == (4379, 88.90)
        assert result["Integration tests"] == (58, 5.51)

    def test_parses_bats_style_line(self):
        """Bats output style: `95 passed (8s)`."""
        log = """
━━━ RECAP ━━━

  ✓ Bash tests               95 passed (8s)
  ✓ Plugin tests             6 passed (25s)
"""
        result = U.parse_recap(log)
        assert result["Bash tests"] == (95, 8.0)
        assert result["Plugin tests"] == (6, 25.0)

    def test_parses_mixed_failed_and_passed(self):
        """A suite with failures: `1 failed, 69 passed in 689.24s` → 70."""
        log = """
━━━ RECAP ━━━

  ✗ Live tests               1 failed, 69 passed in 689.24s
"""
        result = U.parse_recap(log)
        assert result["Live tests"] == (70, 689.24)

    def test_deselected_tests_are_not_counted(self):
        """Deselected tests don't consume runtime and should NOT be
        counted as executed."""
        log = """
━━━ RECAP ━━━

  ✗ Functional tests         1 failed, 37 passed, 1 skipped, 68 deselected in 2342.62s
"""
        result = U.parse_recap(log)
        # 1 failed + 37 passed + 1 skipped = 39 executed (deselected excluded)
        assert result["Functional tests"] == (39, 2342.62)

    def test_ignores_non_recap_content(self):
        """Random pytest output before the recap block is ignored."""
        log = """
========================= test session starts ==========================
collected 4379 items
...
4379 passed in 88.90s

━━━ RECAP ━━━

  ✓ Unit tests               4379 passed in 88.90s
"""
        result = U.parse_recap(log)
        # Only the tier from inside the RECAP block is captured.
        assert list(result.keys()) == ["Unit tests"]


# ---------------------------------------------------------------------------
# update_json_file — read existing JSON, merge, write back
# ---------------------------------------------------------------------------


class TestUpdateJsonFile:
    def test_creates_file_when_missing(self, tmp_path):
        target = tmp_path / "test_times.json"
        updates = {"Unit tests": (4379, 88.90)}
        U.update_json_file(target, updates)
        data = json.loads(target.read_text())
        entry = data["Unit tests"]
        assert entry["count"] == 4379
        assert entry["avg_seconds"] == pytest.approx(88.90 / 4379, rel=1e-3)

    def test_preserves_unrelated_tiers(self, tmp_path):
        target = tmp_path / "test_times.json"
        target.write_text(json.dumps({
            "Extended tests": {"count": 9, "avg_seconds": 167.89},
        }))
        updates = {"Unit tests": (4379, 88.90)}
        U.update_json_file(target, updates)
        data = json.loads(target.read_text())
        assert "Extended tests" in data
        assert "Unit tests" in data

    def test_overwrites_existing_tier(self, tmp_path):
        target = tmp_path / "test_times.json"
        target.write_text(json.dumps({
            "Unit tests": {"count": 1, "avg_seconds": 99.0},
        }))
        updates = {"Unit tests": (4379, 88.90)}
        U.update_json_file(target, updates)
        data = json.loads(target.read_text())
        assert data["Unit tests"]["count"] == 4379
