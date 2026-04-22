"""Tests for ``${session:*}`` tokens in MCP server config.

Business requirement: MCP server configs can reference per-session
values via ``${session:workspace}`` and ``${session:id}``. Unlike
``${env:VAR}`` (substituted at parse time against kiso's process env),
session tokens are preserved verbatim by the parser and resolved
per-call by the manager so that two sessions using the same server
see different subprocess args/env.

Contract:
- Parser accepts ``${session:workspace}`` and ``${session:id}`` in
  any string field (``command``, ``args[*]``, ``cwd``, ``env.*``,
  ``url``, ``headers.*``, ``auth.*``) and leaves them unchanged.
- ``MCPServer.is_session_scoped`` is ``True`` when any string field
  contains a session token.
- ``resolve_session_tokens(server, session, workspace)`` returns a
  shallow-copied ``MCPServer`` with every occurrence substituted.
  For a server that is not session-scoped, the original object is
  returned (identity preserved).
- Unknown ``${session:foo}`` tokens raise ``MCPConfigError`` at
  resolve time (typo guard).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.mcp.config import (
    MCPConfigError,
    MCPServer,
    parse_mcp_section,
    resolve_session_tokens,
)


class TestParsePreservesSessionTokens:
    def test_session_workspace_in_args_kept_verbatim(self):
        servers = parse_mcp_section(
            {
                "fs": {
                    "transport": "stdio",
                    "command": "mcp-filesystem",
                    "args": ["--root", "${session:workspace}"],
                }
            }
        )
        assert servers["fs"].args == ["--root", "${session:workspace}"]

    def test_session_id_in_env_kept_verbatim(self):
        servers = parse_mcp_section(
            {
                "x": {
                    "transport": "stdio",
                    "command": "server",
                    "env": {"SESSION_ID": "${session:id}"},
                }
            }
        )
        assert servers["x"].env == {"SESSION_ID": "${session:id}"}

    def test_session_workspace_in_cwd_kept_verbatim(self):
        servers = parse_mcp_section(
            {
                "y": {
                    "transport": "stdio",
                    "command": "server",
                    "cwd": "${session:workspace}",
                }
            }
        )
        assert servers["y"].cwd == "${session:workspace}"

    def test_session_token_in_http_url_kept_verbatim(self):
        servers = parse_mcp_section(
            {
                "h": {
                    "transport": "http",
                    "url": "https://api.example/${session:id}",
                }
            }
        )
        assert servers["h"].url == "https://api.example/${session:id}"

    def test_env_tokens_still_substituted_alongside_session_tokens(
        self, monkeypatch
    ):
        monkeypatch.setenv("ROOT", "/srv")
        servers = parse_mcp_section(
            {
                "z": {
                    "transport": "stdio",
                    "command": "server",
                    "args": [
                        "--base",
                        "${env:ROOT}",
                        "--work",
                        "${session:workspace}",
                    ],
                }
            }
        )
        assert servers["z"].args == [
            "--base",
            "/srv",
            "--work",
            "${session:workspace}",
        ]


class TestIsSessionScoped:
    def test_plain_server_is_not_session_scoped(self):
        srv = parse_mcp_section(
            {"s": {"transport": "stdio", "command": "x"}}
        )["s"]
        assert srv.is_session_scoped is False

    def test_session_token_anywhere_marks_server_session_scoped(self):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "stdio",
                    "command": "x",
                    "args": ["${session:workspace}"],
                }
            }
        )["s"]
        assert srv.is_session_scoped is True

    def test_session_token_in_env_marks_server_session_scoped(self):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "stdio",
                    "command": "x",
                    "env": {"SID": "${session:id}"},
                }
            }
        )["s"]
        assert srv.is_session_scoped is True

    def test_session_token_in_http_url(self):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "http",
                    "url": "https://api/${session:id}",
                }
            }
        )["s"]
        assert srv.is_session_scoped is True


class TestResolveSessionTokens:
    def test_returns_identical_object_when_not_session_scoped(self):
        srv = parse_mcp_section(
            {"s": {"transport": "stdio", "command": "x"}}
        )["s"]
        resolved = resolve_session_tokens(srv, "sess-A", Path("/tmp/ws"))
        assert resolved is srv

    def test_substitutes_workspace_and_id(self, tmp_path):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "stdio",
                    "command": "${session:workspace}/run",
                    "args": ["--id", "${session:id}"],
                    "cwd": "${session:workspace}",
                    "env": {"SID": "${session:id}"},
                }
            }
        )["s"]
        resolved = resolve_session_tokens(srv, "sess-A", tmp_path)
        assert resolved.command == f"{tmp_path}/run"
        assert resolved.args == ["--id", "sess-A"]
        assert resolved.cwd == str(tmp_path)
        assert resolved.env == {"SID": "sess-A"}

    def test_substitutes_http_fields(self, tmp_path):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "http",
                    "url": "https://api/${session:id}",
                    "headers": {"X-Workspace": "${session:workspace}"},
                }
            }
        )["s"]
        resolved = resolve_session_tokens(srv, "sess-B", tmp_path)
        assert resolved.url == "https://api/sess-B"
        assert resolved.headers == {"X-Workspace": str(tmp_path)}

    def test_unknown_session_token_raises(self):
        srv = MCPServer(
            name="s",
            transport="stdio",
            command="x",
            args=["${session:unknown}"],
        )
        with pytest.raises(MCPConfigError) as exc:
            resolve_session_tokens(srv, "sess-A", Path("/tmp/ws"))
        assert "${session:unknown}" in str(exc.value)

    def test_resolve_preserves_non_session_fields(self, tmp_path):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "stdio",
                    "command": "server",
                    "args": ["${session:workspace}"],
                    "timeout_s": 42.0,
                    "enabled": False,
                }
            }
        )["s"]
        resolved = resolve_session_tokens(srv, "sess-A", tmp_path)
        assert resolved.timeout_s == 42.0
        assert resolved.enabled is False
        assert resolved.name == "s"
        assert resolved.transport == "stdio"
