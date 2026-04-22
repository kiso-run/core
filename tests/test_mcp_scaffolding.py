"""Scaffolding tests for the MCP client package.

Covers:
- Error class hierarchy
- Dataclass shapes (frozen, required fields)
- MCPServer config parsing from raw TOML dict
- ${env:VAR} expansion in string fields
- KISO_* deny-list in per-server env dict
- MCPClient abstract base class cannot be instantiated
- Config integration: kiso.config.Config exposes mcp_servers
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError, fields
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestMCPErrorHierarchy:
    def test_all_errors_inherit_from_mcp_error(self):
        from kiso.mcp.schemas import (
            MCPCapError,
            MCPError,
            MCPInvocationError,
            MCPProtocolError,
            MCPTransportError,
        )

        assert issubclass(MCPProtocolError, MCPError)
        assert issubclass(MCPTransportError, MCPError)
        assert issubclass(MCPInvocationError, MCPError)
        assert issubclass(MCPCapError, MCPError)

    def test_mcp_error_is_exception(self):
        from kiso.mcp.schemas import MCPError

        assert issubclass(MCPError, Exception)

    def test_protocol_error_carries_message(self):
        from kiso.mcp.schemas import MCPProtocolError

        err = MCPProtocolError("bad handshake")
        assert "bad handshake" in str(err)

    def test_transport_error_carries_message(self):
        from kiso.mcp.schemas import MCPTransportError

        err = MCPTransportError("stdin closed")
        assert "stdin closed" in str(err)

    def test_invocation_error_carries_message(self):
        from kiso.mcp.schemas import MCPInvocationError

        err = MCPInvocationError("method not found")
        assert "method not found" in str(err)

    def test_cap_error_carries_message(self):
        from kiso.mcp.schemas import MCPCapError

        err = MCPCapError("sampling not supported")
        assert "sampling not supported" in str(err)


# ---------------------------------------------------------------------------
# Dataclass shapes
# ---------------------------------------------------------------------------


class TestMCPMethod:
    def test_construction_with_required_fields(self):
        from kiso.mcp.schemas import MCPMethod

        m = MCPMethod(
            server="gitlab",
            name="create_issue",
            title=None,
            description="Create a new issue",
            input_schema={"type": "object", "properties": {}},
            output_schema=None,
            annotations=None,
        )
        assert m.server == "gitlab"
        assert m.name == "create_issue"
        assert m.description == "Create a new issue"
        assert m.input_schema == {"type": "object", "properties": {}}

    def test_frozen(self):
        from kiso.mcp.schemas import MCPMethod

        m = MCPMethod(
            server="gitlab",
            name="create_issue",
            title=None,
            description="",
            input_schema={},
            output_schema=None,
            annotations=None,
        )
        with pytest.raises(FrozenInstanceError):
            m.name = "other"  # type: ignore[misc]

    def test_qualified_name_property(self):
        """ will use 'server:method' as the canonical qualified form.
        Expose it on the dataclass so all consumers agree."""
        from kiso.mcp.schemas import MCPMethod

        m = MCPMethod(
            server="gitlab",
            name="create_issue",
            title=None,
            description="",
            input_schema={},
            output_schema=None,
            annotations=None,
        )
        assert m.qualified == "gitlab:create_issue"


class TestMCPServerInfo:
    def test_construction(self):
        from kiso.mcp.schemas import MCPServerInfo

        info = MCPServerInfo(
            name="gitlab",
            title="GitLab MCP",
            version="1.0.0",
            protocol_version="2025-06-18",
            capabilities={"tools": {"listChanged": True}},
            instructions="Use for gitlab operations",
        )
        assert info.name == "gitlab"
        assert info.protocol_version == "2025-06-18"
        assert info.capabilities["tools"]["listChanged"] is True

    def test_frozen(self):
        from kiso.mcp.schemas import MCPServerInfo

        info = MCPServerInfo(
            name="gitlab",
            title=None,
            version="1.0.0",
            protocol_version="2025-06-18",
            capabilities={},
            instructions=None,
        )
        with pytest.raises(FrozenInstanceError):
            info.name = "other"  # type: ignore[misc]


class TestMCPCallResult:
    def test_construction_with_defaults(self):
        from kiso.mcp.schemas import MCPCallResult

        r = MCPCallResult(
            stdout_text="hello",
            published_files=[],
            structured_content=None,
            is_error=False,
        )
        assert r.stdout_text == "hello"
        assert r.published_files == []
        assert r.structured_content is None
        assert r.is_error is False

    def test_is_error_true(self):
        from kiso.mcp.schemas import MCPCallResult

        r = MCPCallResult(
            stdout_text="boom",
            published_files=[],
            structured_content=None,
            is_error=True,
        )
        assert r.is_error is True


# ---------------------------------------------------------------------------
# MCPClient abstract base
# ---------------------------------------------------------------------------


class TestMCPClientABC:
    def test_cannot_instantiate_directly(self):
        from kiso.mcp.client import MCPClient

        with pytest.raises(TypeError):
            MCPClient()  # type: ignore[abstract]

    def test_subclass_must_implement_all_methods(self):
        from kiso.mcp.client import MCPClient

        class Incomplete(MCPClient):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_complete_subclass_can_instantiate(self):
        from kiso.mcp.client import MCPClient
        from kiso.mcp.schemas import MCPCallResult, MCPServerInfo

        class FakeClient(MCPClient):
            async def initialize(self) -> MCPServerInfo:
                return MCPServerInfo(
                    name="fake",
                    title=None,
                    version="0",
                    protocol_version="2025-06-18",
                    capabilities={},
                    instructions=None,
                )

            async def list_methods(self) -> list:
                return []

            async def call_method(self, name: str, args: dict) -> MCPCallResult:
                return MCPCallResult(
                    stdout_text="",
                    published_files=[],
                    structured_content=None,
                    is_error=False,
                )

            async def cancel(self, request_id: Any) -> None:
                return None

            async def shutdown(self) -> None:
                return None

            def is_healthy(self) -> bool:
                return True

        client = FakeClient()
        assert client.is_healthy() is True


# ---------------------------------------------------------------------------
# MCPServer config parsing
# ---------------------------------------------------------------------------


class TestMCPServerConfig:
    def test_parse_valid_stdio_minimal(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "github": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
            }
        }
        servers = parse_mcp_section(raw)
        assert "github" in servers
        srv = servers["github"]
        assert srv.name == "github"
        assert srv.transport == "stdio"
        assert srv.command == "npx"
        assert srv.args == ["-y", "@modelcontextprotocol/server-github"]
        assert srv.env == {}
        assert srv.cwd is None
        assert srv.enabled is True
        assert srv.timeout_s == 60.0

    def test_parse_valid_stdio_full(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "gsc": {
                "transport": "stdio",
                "command": "/opt/gsc/.venv/bin/python",
                "args": ["/opt/gsc/server.py"],
                "env": {"FOO": "bar"},
                "cwd": "/opt/gsc",
                "enabled": True,
                "timeout_s": 120.0,
            }
        }
        servers = parse_mcp_section(raw)
        srv = servers["gsc"]
        assert srv.cwd == "/opt/gsc"
        assert srv.env == {"FOO": "bar"}
        assert srv.timeout_s == 120.0

    def test_parse_valid_http_minimal(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "maps": {
                "transport": "http",
                "url": "https://mapstools.googleapis.com/mcp",
            }
        }
        servers = parse_mcp_section(raw)
        srv = servers["maps"]
        assert srv.transport == "http"
        assert srv.url == "https://mapstools.googleapis.com/mcp"
        assert srv.headers == {}
        assert srv.command is None

    def test_parse_valid_http_full(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "maps": {
                "transport": "http",
                "url": "https://mapstools.googleapis.com/mcp",
                "headers": {"X-Goog-Api-Key": "fake-key"},
                "auth": {"type": "api_key"},
                "timeout_s": 30.0,
            }
        }
        servers = parse_mcp_section(raw)
        srv = servers["maps"]
        assert srv.headers == {"X-Goog-Api-Key": "fake-key"}
        assert srv.auth == {"type": "api_key"}

    def test_empty_section(self):
        from kiso.mcp.config import parse_mcp_section

        assert parse_mcp_section({}) == {}

    def test_none_section(self):
        from kiso.mcp.config import parse_mcp_section

        assert parse_mcp_section(None) == {}

    def test_reject_invalid_transport(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {"bad": {"transport": "websocket", "command": "foo"}}
        with pytest.raises(MCPConfigError, match="transport"):
            parse_mcp_section(raw)

    def test_reject_missing_transport(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {"bad": {"command": "foo"}}
        with pytest.raises(MCPConfigError, match="transport"):
            parse_mcp_section(raw)

    def test_reject_stdio_without_command(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {"bad": {"transport": "stdio"}}
        with pytest.raises(MCPConfigError, match="command"):
            parse_mcp_section(raw)

    def test_reject_http_without_url(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {"bad": {"transport": "http"}}
        with pytest.raises(MCPConfigError, match="url"):
            parse_mcp_section(raw)

    def test_reject_invalid_name(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {"Bad-Name!": {"transport": "stdio", "command": "foo"}}
        with pytest.raises(MCPConfigError, match="name"):
            parse_mcp_section(raw)

    def test_reject_name_starting_with_digit(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {"1bad": {"transport": "stdio", "command": "foo"}}
        with pytest.raises(MCPConfigError, match="name"):
            parse_mcp_section(raw)

    def test_disabled_server_still_parsed(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "github": {
                "transport": "stdio",
                "command": "npx",
                "enabled": False,
            }
        }
        servers = parse_mcp_section(raw)
        assert servers["github"].enabled is False

    def test_timeout_s_must_be_positive(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {
            "bad": {
                "transport": "stdio",
                "command": "foo",
                "timeout_s": -1.0,
            }
        }
        with pytest.raises(MCPConfigError, match="timeout"):
            parse_mcp_section(raw)


# ---------------------------------------------------------------------------
# ${env:VAR} expansion
# ---------------------------------------------------------------------------


class TestEnvExpansion:
    def test_expand_in_command(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {"s": {"transport": "stdio", "command": "${env:MY_CMD}"}}
        with patch.dict(os.environ, {"MY_CMD": "/usr/bin/python"}):
            servers = parse_mcp_section(raw)
        assert servers["s"].command == "/usr/bin/python"

    def test_expand_in_args(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "python",
                "args": ["-u", "${env:MY_SCRIPT}"],
            }
        }
        with patch.dict(os.environ, {"MY_SCRIPT": "/opt/foo/srv.py"}):
            servers = parse_mcp_section(raw)
        assert servers["s"].args == ["-u", "/opt/foo/srv.py"]

    def test_expand_in_env_values(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "foo",
                "env": {"TOKEN": "${env:MY_TOKEN}"},
            }
        }
        with patch.dict(os.environ, {"MY_TOKEN": "ghp_abc"}):
            servers = parse_mcp_section(raw)
        assert servers["s"].env == {"TOKEN": "ghp_abc"}

    def test_expand_in_cwd(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "foo",
                "cwd": "${env:HOME}/tools",
            }
        }
        with patch.dict(os.environ, {"HOME": "/home/me"}):
            servers = parse_mcp_section(raw)
        assert servers["s"].cwd == "/home/me/tools"

    def test_expand_in_url(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "http",
                "url": "${env:MCP_URL}",
            }
        }
        with patch.dict(os.environ, {"MCP_URL": "https://example.com/mcp"}):
            servers = parse_mcp_section(raw)
        assert servers["s"].url == "https://example.com/mcp"

    def test_expand_in_headers(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"X-API-Key": "${env:MY_KEY}"},
            }
        }
        with patch.dict(os.environ, {"MY_KEY": "secret"}):
            servers = parse_mcp_section(raw)
        assert servers["s"].headers == {"X-API-Key": "secret"}

    def test_expand_missing_env_raises(self):
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {
            "s": {"transport": "stdio", "command": "${env:DOES_NOT_EXIST_XYZ}"}
        }
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(MCPConfigError, match="DOES_NOT_EXIST_XYZ"):
                parse_mcp_section(raw)

    def test_no_expansion_on_plain_string(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "plain-string"],
            }
        }
        servers = parse_mcp_section(raw)
        assert servers["s"].args == ["-y", "plain-string"]

    def test_literal_dollar_sign_not_expanded(self):
        """A plain '$' without the '{env:...}' form passes through unchanged."""
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "npx",
                "args": ["--price", "$5.00"],
            }
        }
        servers = parse_mcp_section(raw)
        assert servers["s"].args == ["--price", "$5.00"]

    def test_multiple_expansions_in_one_string(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "http",
                "url": "${env:PROTO}://${env:HOST}/mcp",
            }
        }
        with patch.dict(os.environ, {"PROTO": "https", "HOST": "example.com"}):
            servers = parse_mcp_section(raw)
        assert servers["s"].url == "https://example.com/mcp"


# ---------------------------------------------------------------------------
# KISO_* deny-list
# ---------------------------------------------------------------------------


class TestKisoEnvDenylist:
    def test_reject_kiso_prefix_in_env(self):
        """User cannot set KISO_* vars in per-server env — prevents leaking
        kiso internal secrets into MCP subprocesses via config."""
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "foo",
                "env": {"KISO_SECRET": "stolen"},
            }
        }
        with pytest.raises(MCPConfigError, match="KISO_"):
            parse_mcp_section(raw)

    def test_reject_kiso_prefix_even_after_expansion(self):
        """Expanded value lands on a KISO_ key: still rejected, because the
        key name is what matters."""
        from kiso.mcp.config import MCPConfigError, parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "foo",
                "env": {"KISO_SECRET": "${env:HOME}"},
            }
        }
        with pytest.raises(MCPConfigError, match="KISO_"):
            parse_mcp_section(raw)

    def test_non_kiso_keys_accepted(self):
        from kiso.mcp.config import parse_mcp_section

        raw = {
            "s": {
                "transport": "stdio",
                "command": "foo",
                "env": {"GITHUB_TOKEN": "ghp_x", "OTHER": "y"},
            }
        }
        servers = parse_mcp_section(raw)
        assert servers["s"].env == {"GITHUB_TOKEN": "ghp_x", "OTHER": "y"}


# ---------------------------------------------------------------------------
# Integration with kiso.config.Config
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    def test_config_has_mcp_servers_field(self):
        from kiso.config import Config

        field_names = {f.name for f in fields(Config)}
        assert "mcp_servers" in field_names

    def test_config_dataclass_default_empty_mcp(self, tmp_path):
        """A config.toml with no [mcp.*] sections yields an empty dict."""
        from kiso.config import load_config

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[tokens]\ncli = "tok"\n\n'
            '[providers.openrouter]\nbase_url = "https://example.com/v1"\n\n'
            '[users.admin]\nrole = "admin"\n\n'
            '[models]\nplanner = "x"\n'
            'reviewer = "x"\n'
            'messenger = "x"\n'
            'briefer = "x"\n'
            'classifier = "x"\n'
            'curator = "x"\n'
            'text = "x"\n\n'
            '[settings]\n'
            'max_plans = 3\n'
            'max_replans = 5\n'
            'max_planner_retries = 6\n'
            'max_review_retries = 3\n'
            'max_worker_retries = 3\n'
            'max_stored_messages = 1000\n'
            'max_output_size = 65536\n'
            'max_llm_calls_per_plan = 20\n'
            'llm_timeout = 180\n'
            'llm_cost_limit = 1.0\n'
            'session_timeout = 3600\n'
            'message_max_length = 4000\n'
            'worker_idle_timeout = 60\n'
            'http_port = 8334\n'
            'http_listen = "127.0.0.1"\n'
            'http_bind = "*"\n'
            'sandbox_user = "nobody"\n'
            'install_timeout = 600\n'
            'kiso_dir_size_limit = 1000000000\n'
            'stuck_intervention_depth = 2\n'
            'briefer_enabled = false\n'
            'briefer_wrapper_filter_threshold = 10\n'
            'webhook_allow_list = []\n'
            'webhook_require_https = true\n'
            'webhook_secret = ""\n'
            'webhook_max_payload = 1048576\n'
        )
        cfg = load_config(cfg_path)
        assert cfg.mcp_servers == {}

    def test_config_parses_mcp_section(self, tmp_path):
        """A config.toml with [mcp.github] populates mcp_servers."""
        from kiso.config import load_config

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[tokens]\ncli = "tok"\n\n'
            '[providers.openrouter]\nbase_url = "https://example.com/v1"\n\n'
            '[users.admin]\nrole = "admin"\n\n'
            '[models]\nplanner = "x"\n'
            'reviewer = "x"\n'
            'messenger = "x"\n'
            'briefer = "x"\n'
            'classifier = "x"\n'
            'curator = "x"\n'
            'text = "x"\n\n'
            '[settings]\n'
            'max_plans = 3\n'
            'max_replans = 5\n'
            'max_planner_retries = 6\n'
            'max_review_retries = 3\n'
            'max_worker_retries = 3\n'
            'max_stored_messages = 1000\n'
            'max_output_size = 65536\n'
            'max_llm_calls_per_plan = 20\n'
            'llm_timeout = 180\n'
            'llm_cost_limit = 1.0\n'
            'session_timeout = 3600\n'
            'message_max_length = 4000\n'
            'worker_idle_timeout = 60\n'
            'http_port = 8334\n'
            'http_listen = "127.0.0.1"\n'
            'http_bind = "*"\n'
            'sandbox_user = "nobody"\n'
            'install_timeout = 600\n'
            'kiso_dir_size_limit = 1000000000\n'
            'stuck_intervention_depth = 2\n'
            'briefer_enabled = false\n'
            'briefer_wrapper_filter_threshold = 10\n'
            'webhook_allow_list = []\n'
            'webhook_require_https = true\n'
            'webhook_secret = ""\n'
            'webhook_max_payload = 1048576\n\n'
            '[mcp.github]\n'
            'transport = "stdio"\n'
            'command = "npx"\n'
            'args = ["-y", "@modelcontextprotocol/server-github"]\n'
        )
        cfg = load_config(cfg_path)
        assert "github" in cfg.mcp_servers
        assert cfg.mcp_servers["github"].transport == "stdio"
        assert cfg.mcp_servers["github"].command == "npx"

    def test_config_invalid_mcp_section_raises(self, tmp_path):
        from kiso.config import load_config

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[tokens]\ncli = "tok"\n\n'
            '[providers.openrouter]\nbase_url = "https://example.com/v1"\n\n'
            '[users.admin]\nrole = "admin"\n\n'
            '[models]\nplanner = "x"\n'
            'reviewer = "x"\n'
            'messenger = "x"\n'
            'briefer = "x"\n'
            'classifier = "x"\n'
            'curator = "x"\n'
            'text = "x"\n\n'
            '[settings]\n'
            'max_plans = 3\n'
            'max_replans = 5\n'
            'max_planner_retries = 6\n'
            'max_review_retries = 3\n'
            'max_worker_retries = 3\n'
            'max_stored_messages = 1000\n'
            'max_output_size = 65536\n'
            'max_llm_calls_per_plan = 20\n'
            'llm_timeout = 180\n'
            'llm_cost_limit = 1.0\n'
            'session_timeout = 3600\n'
            'message_max_length = 4000\n'
            'worker_idle_timeout = 60\n'
            'http_port = 8334\n'
            'http_listen = "127.0.0.1"\n'
            'http_bind = "*"\n'
            'sandbox_user = "nobody"\n'
            'install_timeout = 600\n'
            'kiso_dir_size_limit = 1000000000\n'
            'stuck_intervention_depth = 2\n'
            'briefer_enabled = false\n'
            'briefer_wrapper_filter_threshold = 10\n'
            'webhook_allow_list = []\n'
            'webhook_require_https = true\n'
            'webhook_secret = ""\n'
            'webhook_max_payload = 1048576\n\n'
            '[mcp.bad]\n'
            'transport = "websocket"\n'
        )
        with pytest.raises(SystemExit):
            load_config(cfg_path)


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_package_imports(self):
        """Public API must be reachable from kiso.mcp."""
        from kiso.mcp import (  # noqa: F401
            MCPCallResult,
            MCPCapError,
            MCPClient,
            MCPConfigError,
            MCPError,
            MCPInvocationError,
            MCPMethod,
            MCPProtocolError,
            MCPServer,
            MCPServerInfo,
            MCPTransportError,
            parse_mcp_section,
        )
