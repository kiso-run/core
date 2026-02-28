"""Tests for kiso._version.count_loc (M53)."""

from pathlib import Path

import pytest

from kiso._version import count_loc


class TestLocCounter:
    def test_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / "kiso").mkdir()
        (tmp_path / "kiso" / "a.py").write_text("")
        stats = count_loc(tmp_path)
        assert stats["core"] == 0

    def test_only_comments(self, tmp_path: Path) -> None:
        (tmp_path / "kiso").mkdir()
        (tmp_path / "kiso" / "a.py").write_text("# comment\n# another\n\n   \n")
        stats = count_loc(tmp_path)
        assert stats["core"] == 0

    def test_mixed_content(self, tmp_path: Path) -> None:
        (tmp_path / "kiso").mkdir()
        (tmp_path / "kiso" / "a.py").write_text(
            "# header\nx = 1\n\n# comment\ny = 2\nz = 3\n"
        )
        stats = count_loc(tmp_path)
        assert stats["core"] == 3  # x=1, y=2, z=3

    def test_missing_directory(self, tmp_path: Path) -> None:
        stats = count_loc(tmp_path)
        assert stats["core"] == 0
        assert stats["cli"] == 0
        assert stats["tests"] == 0

    def test_total_equals_sum(self, tmp_path: Path) -> None:
        for sub in ("kiso", "cli", "tests"):
            (tmp_path / sub).mkdir()
        (tmp_path / "kiso" / "a.py").write_text("x = 1\n")
        (tmp_path / "cli" / "b.py").write_text("y = 2\n")
        (tmp_path / "tests" / "c.py").write_text("z = 3\n")
        stats = count_loc(tmp_path)
        assert stats["core"] == 1
        assert stats["cli"] == 1
        assert stats["tests"] == 1
        assert stats["total"] == stats["core"] + stats["cli"] + stats["tests"]
        assert stats["total"] == 3
