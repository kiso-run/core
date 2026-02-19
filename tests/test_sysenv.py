"""Tests for kiso/sysenv.py — system environment context."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiso.config import Config, Provider
from kiso.sysenv import (
    PROBE_BINARIES,
    _collect_binaries,
    _collect_connectors,
    _collect_os_info,
    build_system_env_section,
    collect_system_env,
    get_system_env,
    invalidate_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure every test starts with a clean cache."""
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture()
def config():
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models={"planner": "gpt-4"},
        settings={
            "exec_timeout": 120,
            "max_output_size": 1_048_576,
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
        },
        raw={},
    )


# --- _collect_os_info ---


class TestCollectOsInfo:
    def test_returns_expected_keys(self):
        info = _collect_os_info()
        assert "system" in info
        assert "machine" in info
        assert "release" in info

    def test_values_non_empty(self):
        info = _collect_os_info()
        assert info["system"]
        assert info["machine"]
        assert info["release"]


# --- _collect_binaries ---


class TestCollectBinaries:
    def test_finds_python(self):
        """At least python3 or python should be found in any dev/CI env."""
        found, missing = _collect_binaries()
        assert "python3" in found or "python" in found

    def test_nonexistent_binary_in_missing(self):
        found, missing = _collect_binaries(["__nonexistent_binary_xyz__"])
        assert "__nonexistent_binary_xyz__" in missing
        assert "__nonexistent_binary_xyz__" not in found

    def test_found_and_missing_disjoint(self):
        found, missing = _collect_binaries()
        assert not set(found) & set(missing)


# --- _collect_connectors ---


class TestCollectConnectors:
    def test_empty_when_no_connectors(self):
        with patch("kiso.cli_connector.discover_connectors", return_value=[]):
            result = _collect_connectors()
        assert result == []

    def test_stopped_connector(self, tmp_path):
        """Connector dir exists but no .pid file → stopped."""
        connector_dir = tmp_path / "myconn"
        connector_dir.mkdir()
        connectors = [{"name": "myconn", "platform": "discord", "path": str(connector_dir)}]
        with patch("kiso.cli_connector.discover_connectors", return_value=connectors):
            result = _collect_connectors()
        assert len(result) == 1
        assert result[0]["name"] == "myconn"
        assert result[0]["status"] == "stopped"

    def test_running_connector(self, tmp_path):
        """Connector with .pid file and live process → running."""
        connector_dir = tmp_path / "myconn"
        connector_dir.mkdir()
        (connector_dir / ".pid").write_text("12345")
        connectors = [{"name": "myconn", "platform": "telegram", "path": str(connector_dir)}]

        with patch("kiso.cli_connector.discover_connectors", return_value=connectors), \
             patch("os.kill") as mock_kill:
            # os.kill(pid, 0) succeeds → process alive
            mock_kill.return_value = None
            result = _collect_connectors()

        assert len(result) == 1
        assert result[0]["status"] == "running"
        mock_kill.assert_called_once_with(12345, 0)

    def test_stale_pid_shows_stopped(self, tmp_path):
        """Connector with .pid file but dead process → stopped."""
        connector_dir = tmp_path / "myconn"
        connector_dir.mkdir()
        (connector_dir / ".pid").write_text("99999")
        connectors = [{"name": "myconn", "platform": "discord", "path": str(connector_dir)}]

        with patch("kiso.cli_connector.discover_connectors", return_value=connectors), \
             patch("os.kill", side_effect=ProcessLookupError):
            result = _collect_connectors()

        assert result[0]["status"] == "stopped"


# --- collect_system_env ---


class TestCollectSystemEnv:
    def test_returns_all_expected_keys(self, config):
        with patch("kiso.cli_connector.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        expected_keys = {
            "os", "shell", "exec_cwd", "exec_env", "exec_timeout",
            "max_output_size", "available_binaries", "missing_binaries",
            "connectors", "max_plan_tasks", "max_replan_depth",
        }
        assert expected_keys <= set(env.keys())

    def test_uses_config_settings(self):
        cfg = Config(
            tokens={}, providers={}, users={}, models={}, raw={},
            settings={
                "exec_timeout": 60,
                "max_output_size": 512_000,
                "max_plan_tasks": 10,
                "max_replan_depth": 2,
            },
        )
        with patch("kiso.cli_connector.discover_connectors", return_value=[]):
            env = collect_system_env(cfg)
        assert env["exec_timeout"] == 60
        assert env["max_output_size"] == 512_000
        assert env["max_plan_tasks"] == 10
        assert env["max_replan_depth"] == 2


# --- get_system_env cache ---


class TestGetSystemEnvCache:
    def test_caches_result(self, config):
        """Second call returns cached result without re-collecting."""
        call_count = 0
        original_collect = collect_system_env

        def counting_collect(cfg):
            nonlocal call_count
            call_count += 1
            return original_collect(cfg)

        with patch("kiso.sysenv.collect_system_env", side_effect=counting_collect), \
             patch("kiso.cli_connector.discover_connectors", return_value=[]):
            env1 = get_system_env(config)
            env2 = get_system_env(config)

        assert call_count == 1
        assert env1 is env2

    def test_invalidate_forces_recollection(self, config):
        """invalidate_cache() forces re-collection on next call."""
        call_count = 0
        original_collect = collect_system_env

        def counting_collect(cfg):
            nonlocal call_count
            call_count += 1
            return original_collect(cfg)

        with patch("kiso.sysenv.collect_system_env", side_effect=counting_collect), \
             patch("kiso.cli_connector.discover_connectors", return_value=[]):
            get_system_env(config)
            invalidate_cache()
            get_system_env(config)

        assert call_count == 2

    def test_ttl_expiry_triggers_recollection(self, config):
        """Expired TTL forces re-collection."""
        call_count = 0
        original_collect = collect_system_env
        fake_time = [0.0]

        def counting_collect(cfg):
            nonlocal call_count
            call_count += 1
            return original_collect(cfg)

        def mock_monotonic():
            return fake_time[0]

        with patch("kiso.sysenv.collect_system_env", side_effect=counting_collect), \
             patch("kiso.sysenv.time.monotonic", side_effect=mock_monotonic), \
             patch("kiso.cli_connector.discover_connectors", return_value=[]):
            get_system_env(config)  # t=0, collects
            fake_time[0] = 100.0
            get_system_env(config)  # t=100, cached
            fake_time[0] = 400.0
            get_system_env(config)  # t=400, TTL expired, re-collects

        assert call_count == 2


# --- build_system_env_section ---


class TestBuildSystemEnvSection:
    @pytest.fixture()
    def sample_env(self):
        return {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.17.0-14-generic"},
            "shell": "/bin/sh",
            "exec_cwd": "~/.kiso/sessions/{session}/",
            "exec_env": "PATH only (all other env vars stripped)",
            "exec_timeout": 120,
            "max_output_size": 1_048_576,
            "available_binaries": ["git", "python3", "curl"],
            "missing_binaries": ["docker", "ffmpeg"],
            "connectors": [
                {"name": "discord", "platform": "discord", "status": "running"},
                {"name": "telegram", "platform": "telegram", "status": "stopped"},
            ],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
        }

    def test_contains_os_info(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Linux x86_64 (6.17.0-14-generic)" in section

    def test_contains_available_binaries(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Available binaries: git, python3, curl" in section

    def test_contains_missing_tools(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Missing common tools: docker, ffmpeg" in section

    def test_contains_connectors_with_status(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "discord (running)" in section
        assert "telegram (stopped)" in section

    def test_contains_kiso_cli_section(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Kiso CLI (usable in exec tasks):" in section
        assert "kiso skill list" in section
        assert "kiso connector run" in section

    def test_contains_blocked_commands(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Blocked commands:" in section
        assert "rm -rf" in section

    def test_contains_plan_limits(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "max 20 tasks per plan" in section
        assert "max 3 replans" in section

    def test_no_connectors_says_none(self, sample_env):
        sample_env["connectors"] = []
        section = build_system_env_section(sample_env)
        assert "Connectors: none installed" in section

    def test_no_missing_binaries_omits_line(self, sample_env):
        sample_env["missing_binaries"] = []
        section = build_system_env_section(sample_env)
        assert "Missing common tools" not in section

    def test_max_output_size_formatting(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Max output: 1MB" in section

    def test_max_output_size_kb(self, sample_env):
        sample_env["max_output_size"] = 512 * 1024  # 512KB
        section = build_system_env_section(sample_env)
        assert "Max output: 512KB" in section

    def test_max_output_size_bytes(self, sample_env):
        sample_env["max_output_size"] = 500  # 500B
        section = build_system_env_section(sample_env)
        assert "Max output: 500B" in section

    def test_max_output_size_non_aligned_kb(self, sample_env):
        sample_env["max_output_size"] = 1025  # not aligned to 1024
        section = build_system_env_section(sample_env)
        assert "Max output: 1025B" in section

    def test_max_output_size_multi_mb(self, sample_env):
        sample_env["max_output_size"] = 4 * 1_048_576  # 4MB
        section = build_system_env_section(sample_env)
        assert "Max output: 4MB" in section
