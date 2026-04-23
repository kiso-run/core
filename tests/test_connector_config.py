"""Tests for ``kiso.connector_config`` — parser for ``[connectors.<name>]``
config.toml sections.

The parser mirrors ``kiso.mcp.config.parse_mcp_section``: a small frozen
dataclass per connector, parse-time validation, ``${env:VAR}`` expansion
in string fields, and a ``KISO_*`` env-denylist. Connectors are always
subprocess-launched (no transport switch) and are daemons (no per-call
timeout, no session scoping).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


class TestConnectorConfigParsingValid:
    def test_parse_minimal(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"discord": {"command": "uvx", "args": ["kiso-discord-connector"]}}
        connectors = parse_connectors_section(raw)
        assert "discord" in connectors
        c = connectors["discord"]
        assert c.name == "discord"
        assert c.command == "uvx"
        assert c.args == ["kiso-discord-connector"]
        assert c.env == {}
        assert c.cwd is None
        assert c.token is None
        assert c.webhook is None
        assert c.enabled is True

    def test_parse_full(self):
        from kiso.connector_config import parse_connectors_section

        raw = {
            "discord": {
                "command": "uvx",
                "args": ["kiso-discord-connector", "--verbose"],
                "env": {"DISCORD_TOKEN": "secret"},
                "cwd": "/opt/discord",
                "token": "kiso-api-secret",
                "webhook": "http://localhost:9001/kiso-results",
                "enabled": True,
            }
        }
        c = parse_connectors_section(raw)["discord"]
        assert c.args == ["kiso-discord-connector", "--verbose"]
        assert c.env == {"DISCORD_TOKEN": "secret"}
        assert c.cwd == "/opt/discord"
        assert c.token == "kiso-api-secret"
        assert c.webhook == "http://localhost:9001/kiso-results"

    def test_empty_section(self):
        from kiso.connector_config import parse_connectors_section

        assert parse_connectors_section({}) == {}

    def test_none_section(self):
        from kiso.connector_config import parse_connectors_section

        assert parse_connectors_section(None) == {}

    def test_multiple_connectors(self):
        from kiso.connector_config import parse_connectors_section

        raw = {
            "discord": {"command": "uvx", "args": ["kiso-discord-connector"]},
            "slack": {"command": "python", "args": ["-m", "slack_connector"]},
        }
        connectors = parse_connectors_section(raw)
        assert set(connectors.keys()) == {"discord", "slack"}

    def test_disabled_connector_still_parsed(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"discord": {"command": "uvx", "enabled": False}}
        c = parse_connectors_section(raw)["discord"]
        assert c.enabled is False

    def test_frozen_dataclass(self):
        from kiso.connector_config import parse_connectors_section

        c = parse_connectors_section({"d": {"command": "x"}})["d"]
        with pytest.raises((AttributeError, Exception)):
            c.command = "y"  # type: ignore[misc]


class TestConnectorConfigParsingInvalid:
    def test_reject_missing_command(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"bad": {"args": ["something"]}}
        with pytest.raises(ConnectorConfigError, match="command"):
            parse_connectors_section(raw)

    def test_reject_empty_command(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"bad": {"command": ""}}
        with pytest.raises(ConnectorConfigError, match="command"):
            parse_connectors_section(raw)

    def test_reject_non_string_command(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"bad": {"command": ["uvx", "foo"]}}
        with pytest.raises(ConnectorConfigError, match="command"):
            parse_connectors_section(raw)

    def test_reject_invalid_name(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"Bad-Name!": {"command": "foo"}}
        with pytest.raises(ConnectorConfigError, match="name"):
            parse_connectors_section(raw)

    def test_reject_name_starting_with_digit(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"1discord": {"command": "foo"}}
        with pytest.raises(ConnectorConfigError, match="name"):
            parse_connectors_section(raw)

    def test_reject_section_not_table(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"discord": "not-a-table"}
        with pytest.raises(ConnectorConfigError, match="table"):
            parse_connectors_section(raw)

    def test_reject_top_level_not_table(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        with pytest.raises(ConnectorConfigError, match="table"):
            parse_connectors_section("not-a-dict")  # type: ignore[arg-type]

    def test_reject_args_not_list(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "args": "single-string"}}
        with pytest.raises(ConnectorConfigError, match="args"):
            parse_connectors_section(raw)

    def test_reject_args_non_string_elements(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "args": ["ok", 42]}}
        with pytest.raises(ConnectorConfigError, match="args"):
            parse_connectors_section(raw)

    def test_reject_cwd_not_string(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "cwd": 42}}
        with pytest.raises(ConnectorConfigError, match="cwd"):
            parse_connectors_section(raw)

    def test_reject_token_not_string(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "token": 42}}
        with pytest.raises(ConnectorConfigError, match="token"):
            parse_connectors_section(raw)

    def test_reject_webhook_not_string(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "webhook": []}}
        with pytest.raises(ConnectorConfigError, match="webhook"):
            parse_connectors_section(raw)

    def test_reject_env_not_dict(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "env": "KEY=VAL"}}
        with pytest.raises(ConnectorConfigError, match="env"):
            parse_connectors_section(raw)

    def test_reject_env_non_string_value(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "env": {"KEY": 42}}}
        with pytest.raises(ConnectorConfigError, match="env"):
            parse_connectors_section(raw)

    def test_reject_env_kiso_prefix(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "uvx", "env": {"KISO_SECRET": "value"}}}
        with pytest.raises(ConnectorConfigError, match="KISO_"):
            parse_connectors_section(raw)


class TestConnectorEnvExpansion:
    def test_expand_in_command(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"d": {"command": "${env:MY_BIN}"}}
        with patch.dict(os.environ, {"MY_BIN": "/usr/bin/uvx"}, clear=False):
            c = parse_connectors_section(raw)["d"]
        assert c.command == "/usr/bin/uvx"

    def test_expand_in_args(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"d": {"command": "uvx", "args": ["${env:MY_PKG}"]}}
        with patch.dict(os.environ, {"MY_PKG": "kiso-discord-connector"}, clear=False):
            c = parse_connectors_section(raw)["d"]
        assert c.args == ["kiso-discord-connector"]

    def test_expand_in_env_values(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"d": {"command": "uvx", "env": {"DISCORD_TOKEN": "${env:DISCORD_REAL}"}}}
        with patch.dict(os.environ, {"DISCORD_REAL": "xoxb-fake"}, clear=False):
            c = parse_connectors_section(raw)["d"]
        assert c.env == {"DISCORD_TOKEN": "xoxb-fake"}

    def test_expand_in_cwd(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"d": {"command": "uvx", "cwd": "${env:BASE_DIR}/connector"}}
        with patch.dict(os.environ, {"BASE_DIR": "/opt"}, clear=False):
            c = parse_connectors_section(raw)["d"]
        assert c.cwd == "/opt/connector"

    def test_expand_in_token(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"d": {"command": "uvx", "token": "${env:KISO_CONN_DISCORD}"}}
        with patch.dict(os.environ, {"KISO_CONN_DISCORD": "api-sec"}, clear=False):
            c = parse_connectors_section(raw)["d"]
        assert c.token == "api-sec"

    def test_expand_in_webhook(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"d": {"command": "uvx", "webhook": "http://${env:WH_HOST}:9001/x"}}
        with patch.dict(os.environ, {"WH_HOST": "localhost"}, clear=False):
            c = parse_connectors_section(raw)["d"]
        assert c.webhook == "http://localhost:9001/x"

    def test_missing_env_var_raises(self):
        from kiso.connector_config import ConnectorConfigError, parse_connectors_section

        raw = {"d": {"command": "${env:DEFINITELY_NOT_SET_XYZ}"}}
        # Ensure the variable is really absent.
        env = {k: v for k, v in os.environ.items() if k != "DEFINITELY_NOT_SET_XYZ"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConnectorConfigError, match="DEFINITELY_NOT_SET_XYZ"):
                parse_connectors_section(raw)

    def test_plain_dollar_sign_passes_through(self):
        from kiso.connector_config import parse_connectors_section

        raw = {"d": {"command": "echo $foo ${bar} $$$"}}
        c = parse_connectors_section(raw)["d"]
        assert c.command == "echo $foo ${bar} $$$"


_BASE_CONFIG = """
[tokens]
admin = "tok"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[users.alice]
role = "admin"
"""


class TestConnectorsWiredIntoConfig:
    """The `[connectors.<name>]` table must land on `Config.connectors`
    after `load_config()` runs — parallel to `mcp_servers`."""

    def test_config_exposes_connectors_dict(self, tmp_path):
        from kiso.config import load_config

        cfg_text = _BASE_CONFIG + """
[connectors.discord]
command = "uvx"
args = ["kiso-discord-connector"]
"""
        p = tmp_path / "config.toml"
        p.write_text(cfg_text)
        config = load_config(p)
        assert hasattr(config, "connectors")
        assert "discord" in config.connectors
        c = config.connectors["discord"]
        assert c.command == "uvx"
        assert c.args == ["kiso-discord-connector"]

    def test_config_absent_connectors_section_yields_empty(self, tmp_path):
        from kiso.config import load_config

        p = tmp_path / "config.toml"
        p.write_text(_BASE_CONFIG)
        config = load_config(p)
        assert config.connectors == {}

    def test_config_invalid_connector_reports_error(self, tmp_path, capsys):
        """An invalid [connectors.<name>] section aborts config load with
        a message naming the offending connector."""
        from kiso.config import load_config

        cfg_text = _BASE_CONFIG + """
[connectors.discord]
# command missing — required
args = ["kiso-discord-connector"]
"""
        p = tmp_path / "config.toml"
        p.write_text(cfg_text)
        with pytest.raises(SystemExit):
            load_config(p)
        err = capsys.readouterr().err
        assert "discord" in err
        assert "command" in err
