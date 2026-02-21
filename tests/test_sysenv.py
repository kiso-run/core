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
            "sys_bin_path", "reference_docs_path",
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
        from kiso.config import KISO_DIR
        return {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.17.0-14-generic"},
            "shell": "/bin/sh",
            "exec_cwd": str(KISO_DIR / "sessions"),
            "exec_env": "PATH (sys/bin prepended) + HOME + git/ssh env vars when config exists",
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
            "sys_bin_path": str(KISO_DIR / "sys" / "bin"),
            "reference_docs_path": str(KISO_DIR / "reference"),
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

    def test_session_shows_absolute_workspace_path(self, sample_env):
        """When session is passed, Exec CWD shows absolute path with session name."""
        from kiso.config import KISO_DIR
        section = build_system_env_section(sample_env, session="host@alice")
        expected = str(KISO_DIR / "sessions" / "host@alice")
        assert f"Exec CWD: {expected}" in section

    def test_session_name_in_output(self, sample_env):
        """Session name is shown when passed."""
        section = build_system_env_section(sample_env, session="host@alice")
        assert "Session: host@alice" in section

    def test_no_session_shows_generic_path(self, sample_env):
        """Without session, Exec CWD shows a generic path."""
        section = build_system_env_section(sample_env)
        assert "/<session>/" in section
        assert "Session:" not in section

    def test_contains_sys_bin_line(self, sample_env):
        """Output contains the Sys bin line."""
        section = build_system_env_section(sample_env)
        assert "Sys bin:" in section
        assert "prepended to exec PATH" in section

    def test_contains_reference_docs_line(self, sample_env):
        """Output contains the Reference docs line."""
        section = build_system_env_section(sample_env)
        assert "Reference docs:" in section
        assert "skill/connector authoring guides" in section

    def test_contains_persistent_dir_line(self, sample_env):
        """Output contains the Persistent dir line."""
        section = build_system_env_section(sample_env)
        assert "Persistent dir: ~/.kiso/sys/" in section


# --- _collect_binaries with sys/bin ---


class TestCollectBinariesSysBin:
    def test_probes_sys_bin_when_exists(self, tmp_path):
        """Binaries in sys/bin are found when directory exists."""
        sys_bin = tmp_path / "sys" / "bin"
        sys_bin.mkdir(parents=True)
        # Create a fake binary
        fake_bin = sys_bin / "myfakebin"
        fake_bin.write_text("#!/bin/sh\necho hi")
        fake_bin.chmod(0o755)
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            found, missing = _collect_binaries(["myfakebin"])
        assert "myfakebin" in found

    def test_no_sys_bin_dir_uses_system_path(self, tmp_path):
        """When sys/bin doesn't exist, falls back to system PATH."""
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            found, missing = _collect_binaries(["__nonexistent_xyz__"])
        assert "__nonexistent_xyz__" in missing


# --- collect_system_env new keys ---


class TestCollectSystemEnvNewKeys:
    def test_includes_sys_bin_path(self, config):
        from kiso.config import KISO_DIR
        with patch("kiso.cli_connector.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        assert env["sys_bin_path"] == str(KISO_DIR / "sys" / "bin")

    def test_includes_reference_docs_path(self, config):
        from kiso.config import KISO_DIR
        with patch("kiso.cli_connector.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        assert env["reference_docs_path"] == str(KISO_DIR / "reference")
