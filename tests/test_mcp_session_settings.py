"""Tests for the MCP per-session pool settings.

Business requirement: the manager's idle timeout and LRU bound are
configurable via ``[settings]`` with the same clamp-and-warn pattern
used by every other numeric setting in ``kiso/config.py``.
"""

from __future__ import annotations

from kiso.config import SETTINGS_DEFAULTS, setting_int


class TestDefaults:
    def test_mcp_session_idle_timeout_default(self):
        assert SETTINGS_DEFAULTS["mcp_session_idle_timeout"] == 1800

    def test_mcp_max_session_clients_per_server_default(self):
        assert SETTINGS_DEFAULTS["mcp_max_session_clients_per_server"] == 32


class TestClampingIdleTimeout:
    def test_within_range_kept(self, caplog):
        settings = {"mcp_session_idle_timeout": 600}
        assert setting_int(settings, "mcp_session_idle_timeout", lo=60, hi=7200) == 600

    def test_below_minimum_clamped(self, caplog):
        settings = {"mcp_session_idle_timeout": 10}
        val = setting_int(settings, "mcp_session_idle_timeout", lo=60, hi=7200)
        assert val == 60

    def test_above_maximum_clamped(self, caplog):
        settings = {"mcp_session_idle_timeout": 100_000}
        val = setting_int(settings, "mcp_session_idle_timeout", lo=60, hi=7200)
        assert val == 7200


class TestClampingMaxClients:
    def test_within_range_kept(self, caplog):
        settings = {"mcp_max_session_clients_per_server": 16}
        val = setting_int(
            settings, "mcp_max_session_clients_per_server", lo=1, hi=256
        )
        assert val == 16

    def test_below_minimum_clamped(self, caplog):
        settings = {"mcp_max_session_clients_per_server": 0}
        val = setting_int(
            settings, "mcp_max_session_clients_per_server", lo=1, hi=256
        )
        assert val == 1

    def test_above_maximum_clamped(self, caplog):
        settings = {"mcp_max_session_clients_per_server": 9999}
        val = setting_int(
            settings, "mcp_max_session_clients_per_server", lo=1, hi=256
        )
        assert val == 256
