"""Tests for cli/plugin_test_runner.py (M647)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cli.plugin_test_runner import (
    PluginTestResult,
    _print_report,
    _resolve_filter,
    _test_one_plugin,
    test_plugins as run_plugin_tests,
    main,
)


SAMPLE_REGISTRY = {
    "tools": [
        {"name": "websearch", "description": "Web search"},
        {"name": "browser", "description": "Browser automation"},
    ],
    "connectors": [
        {"name": "discord", "description": "Discord bridge"},
    ],
}


class TestResolveFilter:
    def test_empty_filter_returns_all(self):
        targets = _resolve_filter(SAMPLE_REGISTRY, "")
        names = {(t, n) for t, n in targets}
        assert ("tool", "websearch") in names
        assert ("tool", "browser") in names
        assert ("connector", "discord") in names
        assert len(targets) == 3

    def test_tools_filter(self):
        targets = _resolve_filter(SAMPLE_REGISTRY, "tools")
        assert all(t == "tool" for t, _ in targets)
        assert len(targets) == 2

    def test_connectors_filter(self):
        targets = _resolve_filter(SAMPLE_REGISTRY, "connectors")
        assert all(t == "connector" for t, _ in targets)
        assert len(targets) == 1

    def test_specific_name_autodetects_type(self):
        targets = _resolve_filter(SAMPLE_REGISTRY, "browser")
        assert targets == [("tool", "browser")]

    def test_specific_connector_autodetects(self):
        targets = _resolve_filter(SAMPLE_REGISTRY, "discord")
        assert targets == [("connector", "discord")]

    def test_multiple_names(self):
        targets = _resolve_filter(SAMPLE_REGISTRY, "browser,discord")
        assert ("tool", "browser") in targets
        assert ("connector", "discord") in targets
        assert len(targets) == 2

    def test_unknown_name_skipped(self, capsys):
        targets = _resolve_filter(SAMPLE_REGISTRY, "nonexistent")
        assert targets == []
        assert "not found in registry" in capsys.readouterr().err


class TestTestOnePlugin:
    def test_clone_failure(self, tmp_path):
        with patch("cli.plugin_test_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="repo not found")
            result = _test_one_plugin(tmp_path, "tool", "fake")

        assert result.stage == "clone"
        assert not result.passed
        assert "repo not found" in result.error

    def test_missing_manifest(self, tmp_path):
        plugin_dir = tmp_path / "tool-fake"

        def mock_run(cmd, **kw):
            if "git" in cmd[0]:
                plugin_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0)
            return MagicMock(returncode=1, stderr="fail")

        with patch("cli.plugin_test_runner.subprocess.run", side_effect=mock_run):
            result = _test_one_plugin(tmp_path, "tool", "fake")

        assert result.stage == "validate"
        assert "kiso.toml" in result.error

    def test_install_failure(self, tmp_path):
        plugin_dir = tmp_path / "tool-fake"

        def mock_run(cmd, **kw):
            if "git" in cmd[0]:
                plugin_dir.mkdir(parents=True, exist_ok=True)
                (plugin_dir / "kiso.toml").write_text("[kiso]\nname = 'fake'")
                return MagicMock(returncode=0)
            if "uv" in cmd[0]:
                return MagicMock(returncode=1, stderr="uv sync error")
            return MagicMock(returncode=0)

        with patch("cli.plugin_test_runner.subprocess.run", side_effect=mock_run):
            result = _test_one_plugin(tmp_path, "tool", "fake")

        assert result.stage == "install"
        assert "uv sync" in result.error

    def test_no_tests_dir_skipped(self, tmp_path):
        plugin_dir = tmp_path / "tool-fake"

        def mock_run(cmd, **kw):
            if "git" in cmd[0]:
                plugin_dir.mkdir(parents=True, exist_ok=True)
                (plugin_dir / "kiso.toml").write_text("[kiso]\nname = 'fake'")
                return MagicMock(returncode=0)
            if "uv" in cmd[0] and "sync" in cmd:
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        with patch("cli.plugin_test_runner.subprocess.run", side_effect=mock_run):
            result = _test_one_plugin(tmp_path, "tool", "fake")

        assert result.skipped
        assert result.passed
        assert "no tests/" in result.error

    def test_tests_pass(self, tmp_path):
        plugin_dir = tmp_path / "tool-fake"

        call_count = {"n": 0}

        def mock_run(cmd, **kw):
            call_count["n"] += 1
            if "git" in cmd[0]:
                plugin_dir.mkdir(parents=True, exist_ok=True)
                (plugin_dir / "kiso.toml").write_text("[kiso]\nname = 'fake'")
                tests_dir = plugin_dir / "tests"
                tests_dir.mkdir()
                (tests_dir / "test_basic.py").write_text("def test_ok(): pass")
                return MagicMock(returncode=0)
            if "uv" in cmd[0] and "sync" in cmd:
                return MagicMock(returncode=0)
            if "pytest" in cmd:
                return MagicMock(returncode=0, stdout="3 passed in 0.5s")
            return MagicMock(returncode=0)

        with patch("cli.plugin_test_runner.subprocess.run", side_effect=mock_run):
            result = _test_one_plugin(tmp_path, "tool", "fake")

        assert result.passed
        assert not result.skipped
        assert result.test_count == 3
        assert result.stage == "done"

    def test_tests_fail(self, tmp_path):
        plugin_dir = tmp_path / "tool-fake"

        def mock_run(cmd, **kw):
            if "git" in cmd[0]:
                plugin_dir.mkdir(parents=True, exist_ok=True)
                (plugin_dir / "kiso.toml").write_text("[kiso]\nname = 'fake'")
                tests_dir = plugin_dir / "tests"
                tests_dir.mkdir()
                (tests_dir / "test_basic.py").write_text("def test_ok(): pass")
                return MagicMock(returncode=0)
            if "uv" in cmd[0] and "sync" in cmd:
                return MagicMock(returncode=0)
            if "pytest" in cmd:
                return MagicMock(
                    returncode=1,
                    stdout="FAILED tests/test_basic.py::test_ok\n1 passed, 1 failed in 0.5s",
                )
            return MagicMock(returncode=0)

        with patch("cli.plugin_test_runner.subprocess.run", side_effect=mock_run):
            result = _test_one_plugin(tmp_path, "tool", "fake")

        assert not result.passed
        assert result.stage == "test"
        assert result.test_count == 2  # 1 passed + 1 failed


class TestMain:
    def test_main_all_pass(self):
        fake_result = PluginTestResult(
            name="fake", plugin_type="tool", stage="done",
            passed=True, test_count=3, duration_s=1.0,
        )
        with patch("cli.plugin_test_runner.test_plugins", return_value=[fake_result]):
            assert main("") == 0

    def test_main_some_fail(self):
        results = [
            PluginTestResult(name="a", plugin_type="tool", stage="done", passed=True),
            PluginTestResult(name="b", plugin_type="tool", stage="test", passed=False, error="fail"),
        ]
        with patch("cli.plugin_test_runner.test_plugins", return_value=results):
            assert main("") == 1

    def test_main_skipped_counts_as_pass(self):
        fake_result = PluginTestResult(
            name="fake", plugin_type="tool", stage="done",
            passed=True, skipped=True, error="no tests/",
        )
        with patch("cli.plugin_test_runner.test_plugins", return_value=[fake_result]):
            assert main("") == 0

    def test_main_empty_registry(self):
        with patch("cli.plugin_test_runner.test_plugins", return_value=[]):
            assert main("") == 0


class TestPrintReport:
    """summary shows total test count and elapsed time."""

    def test_summary_shows_total_tests_and_time(self, capsys):
        results = [
            PluginTestResult(name="a", plugin_type="tool", stage="done",
                             passed=True, test_count=50, duration_s=3.0),
            PluginTestResult(name="b", plugin_type="tool", stage="done",
                             passed=True, test_count=30, duration_s=2.5),
        ]
        _print_report(results)
        out = capsys.readouterr().out
        assert "80 tests" in out          # 50 + 30
        assert "2 plugins" in out
        assert "5.5s" in out              # 3.0 + 2.5

    def test_summary_empty_results(self, capsys):
        _print_report([])
        out = capsys.readouterr().out
        assert out == ""
