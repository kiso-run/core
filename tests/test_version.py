"""Tests for kiso._version.count_loc."""

import os
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

    def test_unreadable_file_silently_skipped(self, tmp_path: Path) -> None:
        if os.getuid() == 0:
            pytest.skip("root bypasses file permissions")
        (tmp_path / "kiso").mkdir()
        readable = tmp_path / "kiso" / "readable.py"
        readable.write_text("x = 1\n")
        unreadable = tmp_path / "kiso" / "unreadable.py"
        unreadable.write_text("y = 2\n")
        os.chmod(unreadable, 0o000)
        try:
            stats = count_loc(tmp_path)
            assert stats["core"] == 1  # only readable.py counted
        finally:
            os.chmod(unreadable, 0o644)

