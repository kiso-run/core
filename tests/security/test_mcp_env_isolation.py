"""Concern 3 — MCP session env must not leak across sessions.

A secret set on session A's per-session env (e.g. an OAuth token
resolved in the OAuth flow for user A) must never appear in a
client spawned for session B, even when both point at the same
MCP server.

The pool key ``(server_name, scope_key, sandbox_uid)`` is the
mechanism that enforces this — session-scoped servers land under
``scope_key = <session_id>`` so session A and session B never share
a client. This test fixes that invariant so it can't silently
regress under future refactors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiso.mcp.config import MCPServer, parse_mcp_section
from kiso.mcp.manager import MCPManager
from kiso.mcp.schemas import MCPCallResult, MCPMethod, MCPServerInfo


pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self, server: MCPServer, *, extra_env: dict[str, str] | None = None) -> None:
        self.server = server
        self.extra_env = dict(extra_env or {})
        self._healthy = True
        self._init = False

    async def initialize(self) -> MCPServerInfo:
        self._init = True
        return MCPServerInfo(
            name=self.server.name, title=None, version="1",
            protocol_version="2025-06-18", capabilities={}, instructions=None,
        )

    async def list_methods(self) -> list[MCPMethod]:
        return [MCPMethod(
            server=self.server.name, name="ping", title=None, description="",
            input_schema={"type": "object"}, output_schema=None, annotations=None,
        )]

    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        return MCPCallResult(stdout_text="ok", published_files=[],
                             structured_content=None, is_error=False)

    async def cancel(self, request_id: Any) -> None: ...

    async def shutdown(self) -> None:
        self._healthy = False
        self._init = False

    def is_healthy(self) -> bool:
        return self._healthy and self._init


@pytest.fixture
def recorder():
    created: list[_Recorder] = []

    def factory(server, *, extra_env=None, sandbox_uid=None):
        c = _Recorder(server, extra_env=extra_env)
        created.append(c)
        return c

    factory.created = created  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def workspace_resolver(tmp_path):
    def resolve(sid: str) -> Path:
        p = tmp_path / sid
        p.mkdir(exist_ok=True)
        return p
    return resolve


def _session_scoped_server() -> MCPServer:
    return parse_mcp_section({
        "fs": {
            "transport": "stdio",
            "command": "mcp-fs",
            "args": ["--root", "${session:workspace}"],
        }
    })["fs"]


async def test_session_env_never_leaks_across_sessions(recorder, workspace_resolver):
    mgr = MCPManager(
        {"fs": _session_scoped_server()},
        client_factory=recorder,
        workspace_resolver=workspace_resolver,
    )
    # Session A gets a secret in its per-session env.
    mgr.set_session_env("A", {"OAUTH_TOKEN": "token-for-A"})
    mgr.set_session_env("B", {"OAUTH_TOKEN": "token-for-B"})

    await mgr.call_method("fs", "ping", {}, session="A")
    await mgr.call_method("fs", "ping", {}, session="B")

    # Two distinct clients spawned, one per session.
    assert len(recorder.created) == 2

    # Each client sees ONLY its own session's secret.
    by_token = {c.extra_env.get("OAUTH_TOKEN"): c for c in recorder.created}
    assert "token-for-A" in by_token
    assert "token-for-B" in by_token
    for tok, client in by_token.items():
        # The "other" session's token must not appear in this client's env.
        other = "token-for-B" if tok == "token-for-A" else "token-for-A"
        assert other not in client.extra_env.values()

    await mgr.shutdown_all()
