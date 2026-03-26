"""Tests for kiso/sysenv.py — system environment context."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiso.config import Config, Provider
from kiso.sysenv import (
    PROBE_BINARIES,
    _PKG_MANAGER_MAP,
    _collect_binaries,
    _collect_connectors,
    _collect_os_info,
    _collect_user_info,
    _collect_workspace_files,
    _detect_pkg_manager,
    _load_registry_hints,
    build_install_context,
    build_system_env_essential,
    build_system_env_section,
    collect_system_env,
    get_resource_limits,
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

    def test_m731_distro_key_on_linux(self):
        """M731: _collect_os_info returns distro key when freedesktop_os_release works."""
        fake_release = {"PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)", "ID": "debian", "ID_LIKE": ""}
        with patch("platform.freedesktop_os_release", return_value=fake_release):
            info = _collect_os_info()
        assert info["distro"] == "Debian GNU/Linux 12 (bookworm)"
        assert info["distro_id"] == "debian"
        assert info["pkg_manager"] == "apt"

    def test_m731_distro_missing_on_oserror(self):
        """M731: distro key absent when freedesktop_os_release raises OSError."""
        with patch("platform.freedesktop_os_release", side_effect=OSError):
            info = _collect_os_info()
        assert "distro" not in info
        assert "pkg_manager" not in info

    def test_m731_id_like_fallback(self):
        """M731: pkg_manager detected via ID_LIKE when ID is unknown."""
        fake_release = {"PRETTY_NAME": "Pop!_OS 22.04", "ID": "pop", "ID_LIKE": "ubuntu debian"}
        with patch("platform.freedesktop_os_release", return_value=fake_release):
            info = _collect_os_info()
        assert info["pkg_manager"] == "apt"


# --- _detect_pkg_manager ---


class TestDetectPkgManager:
    def test_debian(self):
        assert _detect_pkg_manager("debian", "") == "apt"

    def test_ubuntu(self):
        assert _detect_pkg_manager("ubuntu", "") == "apt"

    def test_fedora(self):
        assert _detect_pkg_manager("fedora", "") == "dnf"

    def test_alpine(self):
        assert _detect_pkg_manager("alpine", "") == "apk"

    def test_arch(self):
        assert _detect_pkg_manager("arch", "") == "pacman"

    def test_unknown_with_debian_like(self):
        assert _detect_pkg_manager("mylinux", "debian") == "apt"

    def test_completely_unknown(self):
        assert _detect_pkg_manager("unknown", "") is None

    def test_all_mapped_distros_have_value(self):
        """Every entry in _PKG_MANAGER_MAP returns a non-empty string."""
        for distro_id, pkg in _PKG_MANAGER_MAP.items():
            assert _detect_pkg_manager(distro_id, "") == pkg


# --- _collect_user_info ---


class TestCollectUserInfo:
    def test_returns_expected_keys(self):
        info = _collect_user_info()
        assert "user" in info
        assert "is_root" in info
        assert "has_sudo" in info

    def test_root_detection(self):
        """M732: root user detected when uid is 0."""
        import pwd as _pwd
        with patch("os.getuid", return_value=0), \
             patch.object(_pwd, "getpwuid") as mock_pw:
            mock_pw.return_value = MagicMock(pw_name="root")
            info = _collect_user_info()
        assert info["is_root"] is True
        assert info["user"] == "root"

    def test_non_root_detection(self):
        """M732: non-root user detected when uid is not 0."""
        import pwd as _pwd
        with patch("os.getuid", return_value=1000), \
             patch.object(_pwd, "getpwuid") as mock_pw:
            mock_pw.return_value = MagicMock(pw_name="kiso")
            info = _collect_user_info()
        assert info["is_root"] is False
        assert info["user"] == "kiso"

    def test_sudo_detected(self):
        """M732: has_sudo is True when sudo binary exists."""
        with patch("kiso.sysenv.shutil.which", return_value="/usr/bin/sudo"):
            info = _collect_user_info()
        assert info["has_sudo"] is True

    def test_sudo_not_detected(self):
        """M732: has_sudo is False when sudo binary missing."""
        with patch("kiso.sysenv.shutil.which", return_value=None):
            info = _collect_user_info()
        assert info["has_sudo"] is False


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

    def test_kiso_in_probe_list(self):
        """M45: 'kiso' must be in PROBE_BINARIES so the planner sees it as an available binary."""
        assert "kiso" in PROBE_BINARIES

    def test_m370_system_info_tools(self):
        """M370: PROBE_BINARIES includes common system info tools."""
        for tool in ("free", "ps", "uptime", "uname", "id", "hostname", "df"):
            assert tool in PROBE_BINARIES, f"Missing system tool: {tool}"

    def test_m370_ssh_tools(self):
        """M370: PROBE_BINARIES includes SSH tools."""
        for tool in ("ssh", "ssh-keygen", "ssh-keyscan", "scp"):
            assert tool in PROBE_BINARIES, f"Missing SSH tool: {tool}"

    def test_m370_network_tools(self):
        """M370: PROBE_BINARIES includes network tools."""
        for tool in ("ss", "ip", "ping", "dig"):
            assert tool in PROBE_BINARIES, f"Missing network tool: {tool}"

    def test_m370_process_tools(self):
        """M370: PROBE_BINARIES includes process management tools."""
        for tool in ("kill", "pkill"):
            assert tool in PROBE_BINARIES, f"Missing process tool: {tool}"


# --- _collect_connectors ---


class TestCollectConnectors:
    def test_empty_when_no_connectors(self):
        with patch("kiso.connectors.discover_connectors", return_value=[]):
            result = _collect_connectors()
        assert result == []

    def test_stopped_connector(self, tmp_path):
        """Connector dir exists but no .pid file → stopped."""
        connector_dir = tmp_path / "myconn"
        connector_dir.mkdir()
        connectors = [{"name": "myconn", "platform": "discord", "path": str(connector_dir)}]
        with patch("kiso.connectors.discover_connectors", return_value=connectors):
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

        with patch("kiso.connectors.discover_connectors", return_value=connectors), \
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

        with patch("kiso.connectors.discover_connectors", return_value=connectors), \
             patch("os.kill", side_effect=ProcessLookupError):
            result = _collect_connectors()

        assert result[0]["status"] == "stopped"


# --- collect_system_env ---


class TestCollectSystemEnv:
    def test_returns_all_expected_keys(self, config):
        with patch("kiso.connectors.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        expected_keys = {
            "os", "shell", "exec_cwd", "exec_env",
            "max_output_size", "available_binaries", "missing_binaries",
            "connectors", "max_plan_tasks", "max_replan_depth",
            "sys_bin_path", "reference_docs_path", "registry_url",
        }
        assert expected_keys <= set(env.keys())

    def test_uses_config_settings(self):
        cfg = Config(
            tokens={}, providers={}, users={}, models={}, raw={},
            settings={
                "llm_timeout": 60,
                "max_output_size": 512_000,
                "max_plan_tasks": 10,
                "max_replan_depth": 2,
            },
        )
        with patch("kiso.connectors.discover_connectors", return_value=[]):
            env = collect_system_env(cfg)
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
             patch("kiso.connectors.discover_connectors", return_value=[]):
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
             patch("kiso.connectors.discover_connectors", return_value=[]):
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
             patch("kiso.connectors.discover_connectors", return_value=[]):
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
            "registry_url": "https://raw.githubusercontent.com/kiso-run/core/main/registry.json",
            "registry_hints": "browser (Headless browser automation); search (Web search)",
        }

    def test_contains_os_info(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Linux x86_64 (6.17.0-14-generic)" in section

    def test_m731_distro_in_os_line(self, sample_env):
        """M731: distro name appended to OS line when present."""
        sample_env["os"]["distro"] = "Debian GNU/Linux 12 (bookworm)"
        section = build_system_env_section(sample_env)
        assert "— Debian GNU/Linux 12 (bookworm)" in section

    def test_m731_pkg_manager_line(self, sample_env):
        """M731: Package manager line shown when detected."""
        sample_env["os"]["pkg_manager"] = "apt"
        section = build_system_env_section(sample_env)
        assert "Package manager: apt" in section

    def test_m731_no_pkg_manager_line_when_absent(self, sample_env):
        """M731: No Package manager line when not detected."""
        section = build_system_env_section(sample_env)
        assert "Package manager:" not in section

    def test_m732_user_root_sudo_not_needed(self, sample_env):
        """M732: root user shows 'sudo not needed'."""
        sample_env["user_info"] = {"user": "root", "is_root": True, "has_sudo": False}
        section = build_system_env_section(sample_env)
        assert "User: root (sudo not needed" in section

    def test_m732_user_with_sudo(self, sample_env):
        """M732: non-root user with sudo shows 'sudo available'."""
        sample_env["user_info"] = {"user": "kiso", "is_root": False, "has_sudo": True}
        section = build_system_env_section(sample_env)
        assert "User: kiso (sudo available)" in section

    def test_m732_user_without_sudo(self, sample_env):
        """M732: non-root user without sudo shows 'sudo not available'."""
        sample_env["user_info"] = {"user": "kiso", "is_root": False, "has_sudo": False}
        section = build_system_env_section(sample_env)
        assert "User: kiso (sudo not available)" in section

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

    def test_no_kiso_cli_in_full(self, sample_env):
        """M937: Kiso CLI commands removed — covered by kiso_commands module."""
        section = build_system_env_section(sample_env)
        assert "Kiso CLI (usable in exec tasks):" not in section

    def test_no_registry_hints_in_full(self, sample_env):
        """M937: Registry hints removed — covered by registry tools section."""
        section = build_system_env_section(sample_env)
        assert "Registry tools available:" not in section

    def test_contains_blocked_commands(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "Blocked commands:" in section
        assert "rm -rf" in section

    def test_contains_plan_limits(self, sample_env):
        section = build_system_env_section(sample_env)
        assert "max 20 tasks per plan" in section
        assert "max 3 replans" in section
        assert "extendable by planner up to +3" in section

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
        assert "tool/connector authoring guides" in section

    def test_contains_persistent_dir_line(self, sample_env):
        """Output contains the Persistent dir line."""
        section = build_system_env_section(sample_env)
        assert "Persistent dir: ~/.kiso/sys/" in section

    def test_contains_plugin_registry_line(self, sample_env):
        """Output contains the Plugin registry line."""
        section = build_system_env_section(sample_env)
        assert "Plugin registry:" in section
        assert "registry.json" in section

    def test_contains_network_line(self, sample_env):
        """Output contains the Network line."""
        section = build_system_env_section(sample_env)
        assert "Network:" in section
        assert "internet access available" in section

    def test_contains_public_files_line(self, sample_env):
        """Output contains the Public files line."""
        section = build_system_env_section(sample_env)
        assert "Public files:" in section
        assert "pub/" in section


# --- M937: build_system_env_essential ---


class TestBuildSystemEnvEssential:
    """M937: essential system env contains only planner-critical lines."""

    @pytest.fixture()
    def sample_env(self):
        return {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.1"},
            "shell": "/bin/bash",
            "exec_cwd": "/home/test/.kiso/sessions",
            "exec_env": "PATH",
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
            "max_output_size": 1_048_576,
        }

    def test_contains_cwd(self, sample_env):
        section = build_system_env_essential(sample_env, session="s1")
        assert "Exec CWD:" in section

    def test_contains_pub_rule(self, sample_env):
        section = build_system_env_essential(sample_env)
        assert "Public files:" in section
        assert "pub/" in section

    def test_contains_blocked_commands(self, sample_env):
        section = build_system_env_essential(sample_env)
        assert "Blocked commands:" in section

    def test_contains_plan_limits(self, sample_env):
        section = build_system_env_essential(sample_env)
        assert "max 20 tasks" in section
        assert "max 3 replans" in section

    def test_no_os_info(self, sample_env):
        section = build_system_env_essential(sample_env)
        assert "OS:" not in section

    def test_no_binaries(self, sample_env):
        section = build_system_env_essential(sample_env)
        assert "Available binaries:" not in section

    def test_much_shorter_than_full(self, sample_env):
        # Full needs extra keys — add them for comparison
        full_env = {
            **sample_env,
            "exec_env": "PATH",
            "sys_bin_path": "/fake/bin",
            "reference_docs_path": "/fake/docs",
            "registry_url": "https://example.com/registry.json",
            "available_binaries": ["git", "python3"],
            "missing_binaries": [],
            "connectors": [],
        }
        essential = build_system_env_essential(sample_env)
        full = build_system_env_section(full_env)
        assert len(essential) < len(full) * 0.5


# --- M963: build_install_context ---


class TestBuildInstallContext:
    """M963: install context contains only pkg_manager and available_binaries."""

    def test_both_present(self):
        env = {
            "os": {"pkg_manager": "apt"},
            "available_binaries": ["git", "python3", "uv"],
        }
        ctx = build_install_context(env)
        assert "Package manager: apt" in ctx
        assert "Available binaries: git, python3, uv" in ctx

    def test_empty_when_nothing_available(self):
        env = {"os": {}, "available_binaries": []}
        ctx = build_install_context(env)
        assert ctx == ""

    def test_only_pkg_manager(self):
        env = {"os": {"pkg_manager": "dnf"}, "available_binaries": []}
        ctx = build_install_context(env)
        assert "Package manager: dnf" in ctx
        assert "Available binaries" not in ctx

    def test_only_binaries(self):
        env = {"os": {}, "available_binaries": ["curl", "wget"]}
        ctx = build_install_context(env)
        assert "Package manager" not in ctx
        assert "Available binaries: curl, wget" in ctx


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
    def test_m732_includes_user_info(self, config):
        """M732: collect_system_env includes user_info with expected keys."""
        with patch("kiso.connectors.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        assert "user_info" in env
        ui = env["user_info"]
        assert "user" in ui
        assert "is_root" in ui
        assert "has_sudo" in ui
        assert isinstance(ui["is_root"], bool)
        assert isinstance(ui["has_sudo"], bool)

    def test_includes_sys_bin_path(self, config):
        from kiso.config import KISO_DIR
        with patch("kiso.connectors.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        assert env["sys_bin_path"] == str(KISO_DIR / "sys" / "bin")

    def test_includes_reference_docs_path(self, config):
        from kiso.config import KISO_DIR
        with patch("kiso.connectors.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        assert env["reference_docs_path"] == str(KISO_DIR / "reference")

    def test_includes_registry_url(self, config):
        with patch("kiso.connectors.discover_connectors", return_value=[]):
            env = collect_system_env(config)
        assert env["registry_url"] == "https://raw.githubusercontent.com/kiso-run/core/main/registry.json"


# --- _collect_workspace_files ---


class TestCollectWorkspaceFiles:
    def test_workspace_files_listed(self, tmp_path):
        """Files in session workspace appear in listing."""
        session_dir = tmp_path / "sessions" / "test-session"
        session_dir.mkdir(parents=True)
        (session_dir / "report.pdf").write_bytes(b"x" * 2048)
        (session_dir / "notes.txt").write_text("hello")
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = _collect_workspace_files("test-session")
        assert "notes.txt" in result
        assert "report.pdf" in result
        assert "2KB" in result

    def test_workspace_files_empty(self, tmp_path):
        """Empty session dir returns empty string."""
        session_dir = tmp_path / "sessions" / "empty-session"
        session_dir.mkdir(parents=True)
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = _collect_workspace_files("empty-session")
        assert result == ""

    def test_workspace_files_excludes_kiso_dir(self, tmp_path):
        """Files in .kiso/ subdirectory are excluded."""
        session_dir = tmp_path / "sessions" / "test-session"
        session_dir.mkdir(parents=True)
        (session_dir / "visible.txt").write_text("hi")
        kiso_internal = session_dir / ".kiso"
        kiso_internal.mkdir()
        (kiso_internal / "plan_outputs.json").write_text("{}")
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = _collect_workspace_files("test-session")
        assert "visible.txt" in result
        assert "plan_outputs.json" not in result

    def test_workspace_files_truncated_at_30(self, tmp_path):
        """More than 30 files triggers truncation message."""
        session_dir = tmp_path / "sessions" / "big-session"
        session_dir.mkdir(parents=True)
        for i in range(35):
            (session_dir / f"file{i:03d}.txt").write_text("data")
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = _collect_workspace_files("big-session")
        assert "truncated" in result
        # file030-file034 should NOT appear (only first 30 files listed)
        assert "file030.txt" not in result
        assert "file029.txt" in result

    def test_workspace_files_no_session(self, tmp_path):
        """Non-existent session dir returns empty string."""
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = _collect_workspace_files("nonexistent")
        assert result == ""

    def test_workspace_files_scan_cap(self, tmp_path):
        """M37: rglob scan caps at 1000 files to avoid materialising huge dirs."""
        session_dir = tmp_path / "sessions" / "huge-session"
        session_dir.mkdir(parents=True)
        for i in range(1001):
            (session_dir / f"file{i:04d}.txt").write_bytes(b"")
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = _collect_workspace_files("huge-session")
        # Function must return (not hang) and show truncation marker
        assert "truncated" in result


# --- get_resource_limits (M217) ---


class TestGetResourceLimits:
    def test_returns_dict_with_expected_keys(self, tmp_path):
        """get_resource_limits returns a dict with all resource keys."""
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = get_resource_limits()
        for key in ("memory_mb", "memory_used_mb", "cpu_limit",
                     "disk_used_gb", "disk_total_gb", "pids_limit", "pids_used"):
            assert key in result

    def test_reads_memory_from_cgroup(self, tmp_path):
        """Memory limit is read from /sys/fs/cgroup/memory.max."""
        cgroup = tmp_path / "memory.max"
        cgroup.write_text("4294967296\n")  # 4 GB
        with (
            patch("kiso.sysenv.Path") as MockPath,
            patch("kiso.sysenv.KISO_DIR", tmp_path),
            patch("kiso.sysenv.shutil.disk_usage") as mock_du,
        ):
            mock_du.return_value = MagicMock(used=1_000_000_000, total=32_000_000_000)
            def path_side_effect(p):
                if p == "/sys/fs/cgroup/memory.max":
                    return cgroup
                mock = MagicMock()
                mock.read_text.side_effect = OSError("no such file")
                return mock
            MockPath.side_effect = path_side_effect
            result = get_resource_limits()
        assert result["memory_mb"] == 4096

    def test_memory_max_means_unlimited(self, tmp_path):
        """When cgroup says 'max', memory_mb stays None."""
        cgroup = tmp_path / "memory.max"
        cgroup.write_text("max\n")
        with (
            patch("kiso.sysenv.Path") as MockPath,
            patch("kiso.sysenv.KISO_DIR", tmp_path),
            patch("kiso.sysenv.shutil.disk_usage") as mock_du,
        ):
            mock_du.return_value = MagicMock(used=0, total=0)
            def path_side_effect(p):
                if p == "/sys/fs/cgroup/memory.max":
                    return cgroup
                mock = MagicMock()
                mock.read_text.side_effect = OSError("no such file")
                return mock
            MockPath.side_effect = path_side_effect
            result = get_resource_limits()
        assert result["memory_mb"] is None

    def test_reads_cpu_from_cgroup(self, tmp_path):
        """CPU limit is computed from cpu.max quota/period."""
        cgroup = tmp_path / "cpu.max"
        cgroup.write_text("200000 100000\n")  # 2 CPUs
        with (
            patch("kiso.sysenv.Path") as MockPath,
            patch("kiso.sysenv.KISO_DIR", tmp_path),
            patch("kiso.sysenv.shutil.disk_usage") as mock_du,
        ):
            mock_du.return_value = MagicMock(used=0, total=0)
            def path_side_effect(p):
                if p == "/sys/fs/cgroup/cpu.max":
                    return cgroup
                mock = MagicMock()
                mock.read_text.side_effect = OSError("no such file")
                return mock
            MockPath.side_effect = path_side_effect
            result = get_resource_limits()
        assert result["cpu_limit"] == 2.0

    def test_reads_pids_from_cgroup(self, tmp_path):
        """PIDs limit is read from /sys/fs/cgroup/pids.max."""
        cgroup = tmp_path / "pids.max"
        cgroup.write_text("512\n")
        with (
            patch("kiso.sysenv.Path") as MockPath,
            patch("kiso.sysenv.KISO_DIR", tmp_path),
            patch("kiso.sysenv.shutil.disk_usage") as mock_du,
        ):
            mock_du.return_value = MagicMock(used=0, total=0)
            def path_side_effect(p):
                if p == "/sys/fs/cgroup/pids.max":
                    return cgroup
                mock = MagicMock()
                mock.read_text.side_effect = OSError("no such file")
                return mock
            MockPath.side_effect = path_side_effect
            result = get_resource_limits()
        assert result["pids_limit"] == 512

    def test_disk_usage_from_kiso_dir(self, tmp_path):
        """Disk used_gb is actual KISO_DIR size via _kiso_dir_bytes, total_gb from filesystem."""
        with (
            patch("kiso.sysenv.KISO_DIR", tmp_path),
            patch("kiso.worker.utils._kiso_dir_bytes", return_value=3_400_000_000),
            patch("kiso.sysenv.shutil.disk_usage") as mock_du,
        ):
            mock_du.return_value = MagicMock(total=34_000_000_000)
            result = get_resource_limits()
        assert result["disk_used_gb"] == 3.2
        assert result["disk_total_gb"] == 31.7

    def test_no_cgroup_files_returns_none_values(self, tmp_path):
        """When cgroup files don't exist, resource values are None."""
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            result = get_resource_limits()
        # On a normal dev machine, cgroup files may or may not exist;
        # but disk_total should always be present (filesystem capacity)
        assert result["disk_total_gb"] is not None


# --- Workspace lines in build_system_env_section ---


class TestWorkspaceInBuildSection:
    @pytest.fixture()
    def sample_env(self):
        from kiso.config import KISO_DIR
        return {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.17.0"},
            "shell": "/bin/sh",
            "exec_cwd": str(KISO_DIR / "sessions"),
            "exec_env": "PATH",
            "max_output_size": 1_048_576,
            "available_binaries": ["git"],
            "missing_binaries": [],
            "connectors": [],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
            "sys_bin_path": "/tmp/sys/bin",
            "reference_docs_path": "/tmp/reference",
            "registry_url": "https://example.com/registry.json",
        }

    def test_workspace_files_listed_in_output(self, sample_env, tmp_path):
        """When session has files, listing appears in output."""
        session_dir = tmp_path / "sessions" / "ws-test"
        session_dir.mkdir(parents=True)
        (session_dir / "data.csv").write_text("a,b,c")
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            section = build_system_env_section(sample_env, session="ws-test")
        assert "Workspace files: data.csv" in section

    def test_workspace_files_empty_in_output(self, sample_env, tmp_path):
        """Empty workspace shows '(empty)'."""
        session_dir = tmp_path / "sessions" / "empty-ws"
        session_dir.mkdir(parents=True)
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            section = build_system_env_section(sample_env, session="empty-ws")
        assert "Workspace files: (empty)" in section

    def test_workspace_files_no_session(self, sample_env):
        """Without session, no workspace line appears."""
        section = build_system_env_section(sample_env)
        assert "Workspace files:" not in section

    def test_file_search_line_present(self, sample_env, tmp_path):
        """Output contains 'File search:' line when session is provided."""
        session_dir = tmp_path / "sessions" / "search-test"
        session_dir.mkdir(parents=True)
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            section = build_system_env_section(sample_env, session="search-test")
        assert "File search:" in section
        assert "find" in section
        assert "grep" in section

    def test_file_search_line_absent_without_session(self, sample_env):
        """Without session, no file search line appears."""
        section = build_system_env_section(sample_env)
        assert "File search:" not in section

    def test_workspace_excludes_kiso_in_output(self, sample_env, tmp_path):
        """Workspace listing in section excludes .kiso/ files."""
        session_dir = tmp_path / "sessions" / "filter-test"
        session_dir.mkdir(parents=True)
        (session_dir / "report.txt").write_text("hello")
        (session_dir / ".kiso").mkdir()
        (session_dir / ".kiso" / "internal.json").write_text("{}")
        with patch("kiso.sysenv.KISO_DIR", tmp_path):
            section = build_system_env_section(sample_env, session="filter-test")
        assert "report.txt" in section
        assert "internal.json" not in section


class TestM888LoadRegistryHints:
    """M888/M920: _load_registry_hints fetches from online registry."""

    def test_returns_tool_descriptions(self):
        """Fetches online registry and returns tool name + description pairs."""
        registry_data = {
            "tools": [
                {"name": "browser", "description": "Headless browser"},
                {"name": "ocr", "description": "Image OCR"},
            ],
            "connectors": [
                {"name": "discord", "description": "Discord bridge"},
            ],
        }
        with patch("kiso.registry.fetch_registry", return_value=registry_data):
            result = _load_registry_hints()
        assert "browser" in result
        assert "Headless browser" in result
        assert "ocr" in result
        assert "Image OCR" in result
        assert "discord" in result

    def test_returns_empty_when_fetch_fails(self):
        """Returns empty string when online fetch returns empty dict."""
        with patch("kiso.registry.fetch_registry", return_value={}):
            result = _load_registry_hints()
        assert result == ""
