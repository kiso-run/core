"""Tests for ``MCPManager`` per-session client pool.

Business requirement: two user sessions that call the same MCP
server must end up with independent subprocesses when the server
config uses ``${session:*}`` tokens, and share a single subprocess
otherwise. Session secrets set on session A must never reach a
client spawned for session B or a global-scope client.

Contract:
- ``call_method(..., session=None)`` routes to the global-scope
  pool entry; ``call_method(..., session="X")`` routes to the
  per-session pool entry *only if* the server is session-scoped —
  a plain server shares the global client regardless of ``session``.
- Pool is keyed on ``(server_name, scope_key)`` where ``scope_key``
  is ``"_global"`` for global-scope and ``session`` otherwise.
- At spawn time, session-scoped clients receive a server with
  ``${session:workspace}`` and ``${session:id}`` resolved against
  the calling session's workspace.
- ``set_session_env(session, env)`` sets per-session env to merge
  into any session-scoped client spawned for that session. Setting
  it for a session has no effect on any global-scope client. A
  session-scoped client retains the env it got at spawn time.
- ``shutdown_session(session)`` shuts down every pool entry keyed
  to that session without touching global or other-session entries.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kiso.mcp.config import MCPServer, parse_mcp_section
from kiso.mcp.manager import MCPManager
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPMethod,
    MCPServerInfo,
)


def _info(name: str = "s") -> MCPServerInfo:
    return MCPServerInfo(
        name=name,
        title=None,
        version="1.0",
        protocol_version="2025-06-18",
        capabilities={},
        instructions=None,
    )


def _method(name: str, server: str = "s") -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description="",
        input_schema={"type": "object"},
        output_schema=None,
        annotations=None,
    )


class RecordingClient:
    """MCPClient stand-in that records the server config it was
    constructed from, including any resolved session-token args."""

    def __init__(self, server: MCPServer, *, extra_env: dict[str, str] | None = None) -> None:
        self.server = server
        self.extra_env = dict(extra_env or {})
        self._healthy = True
        self._initialized = False
        self._shutdown_called = False
        self.call_log: list[tuple[str, dict]] = []

    async def initialize(self) -> MCPServerInfo:
        self._initialized = True
        return _info(self.server.name)

    async def list_methods(self) -> list[MCPMethod]:
        return [_method("echo", self.server.name)]

    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        self.call_log.append((name, dict(args)))
        return MCPCallResult(
            stdout_text=f"{self.server.name}/{name}",
            published_files=[],
            structured_content=None,
            is_error=False,
        )

    async def cancel(self, request_id: Any) -> None:
        pass

    async def shutdown(self) -> None:
        self._shutdown_called = True
        self._initialized = False
        self._healthy = False

    def is_healthy(self) -> bool:
        return self._healthy and self._initialized


@pytest.fixture
def recorder():
    """Factory that records every client created, with its resolved server."""
    created: list[RecordingClient] = []

    def factory(
        server: MCPServer,
        *,
        extra_env: dict[str, str] | None = None,
        sandbox_uid: int | None = None,
    ) -> RecordingClient:
        c = RecordingClient(server, extra_env=extra_env)
        created.append(c)
        return c

    factory.created = created  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def workspace_resolver(tmp_path):
    """Return a resolver that maps a session id to tmp_path/<id>."""

    def resolver(session_id: str) -> Path:
        p = tmp_path / session_id
        p.mkdir(exist_ok=True)
        return p

    return resolver


def _session_server(name: str = "fs") -> MCPServer:
    return parse_mcp_section(
        {
            name: {
                "transport": "stdio",
                "command": "mcp-filesystem",
                "args": ["--root", "${session:workspace}"],
                "env": {"SESSION_ID": "${session:id}"},
            }
        }
    )[name]


def _plain_server(name: str = "echo") -> MCPServer:
    return parse_mcp_section(
        {
            name: {"transport": "stdio", "command": "echo-server"}
        }
    )[name]


class TestGlobalVsSessionPoolKeys:
    async def test_plain_server_shares_client_across_sessions(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"echo": _plain_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("echo", "echo", {}, session="A")
        await mgr.call_method("echo", "echo", {}, session="B")
        assert len(recorder.created) == 1
        await mgr.shutdown_all()

    async def test_session_scoped_server_spawns_per_session(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("fs", "read", {}, session="A")
        await mgr.call_method("fs", "read", {}, session="B")
        assert len(recorder.created) == 2

        # Each spawned client received its own resolved workspace.
        ws_args = [
            c.server.args[c.server.args.index("--root") + 1]
            for c in recorder.created
        ]
        assert ws_args[0].endswith("/A")
        assert ws_args[1].endswith("/B")
        assert recorder.created[0].server.env["SESSION_ID"] == "A"
        assert recorder.created[1].server.env["SESSION_ID"] == "B"
        await mgr.shutdown_all()

    async def test_second_call_same_session_reuses_client(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("fs", "read", {}, session="A")
        await mgr.call_method("fs", "read", {}, session="A")
        assert len(recorder.created) == 1
        await mgr.shutdown_all()

    async def test_no_session_arg_still_works_for_session_scoped_server(
        self, recorder, workspace_resolver
    ):
        """Backward-compat: ``call_method`` without ``session`` defaults
        to global scope. For a session-scoped server this means the
        session tokens are NOT substituted — the caller got what it
        asked for (global), and the tokens remain verbatim.
        """
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("fs", "read", {})
        assert len(recorder.created) == 1
        # Without a session, the tokens stay as-is.
        assert "${session:workspace}" in recorder.created[0].server.args
        await mgr.shutdown_all()


class TestSessionSecrets:
    async def test_set_session_env_reaches_session_scoped_spawn(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        mgr.set_session_env("A", {"SECRET": "alpha"})
        await mgr.call_method("fs", "read", {}, session="A")
        assert recorder.created[0].extra_env == {"SECRET": "alpha"}
        await mgr.shutdown_all()

    async def test_session_a_secret_does_not_leak_to_session_b(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        mgr.set_session_env("A", {"SECRET": "alpha"})
        mgr.set_session_env("B", {"SECRET": "beta"})
        await mgr.call_method("fs", "read", {}, session="A")
        await mgr.call_method("fs", "read", {}, session="B")
        assert recorder.created[0].extra_env == {"SECRET": "alpha"}
        assert recorder.created[1].extra_env == {"SECRET": "beta"}
        await mgr.shutdown_all()

    async def test_session_env_does_not_reach_global_scope_client(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"echo": _plain_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        mgr.set_session_env("A", {"SECRET": "alpha"})
        await mgr.call_method("echo", "echo", {}, session="A")
        # Plain server pools globally — session env must not leak in.
        assert recorder.created[0].extra_env == {}
        await mgr.shutdown_all()


class TestShutdownSession:
    async def test_shutdown_session_kills_only_that_sessions_clients(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server(), "echo": _plain_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("fs", "read", {}, session="A")
        await mgr.call_method("fs", "read", {}, session="B")
        await mgr.call_method("echo", "echo", {}, session="A")
        # 2 session-scoped (A, B) + 1 global (echo) = 3 clients
        assert len(recorder.created) == 3

        await mgr.shutdown_session("A")

        # Only the A-scoped client was shut down; B-scoped + global stay.
        a_clients = [
            c for c in recorder.created
            if c.server.env.get("SESSION_ID") == "A"
        ]
        b_clients = [
            c for c in recorder.created
            if c.server.env.get("SESSION_ID") == "B"
        ]
        global_clients = [
            c for c in recorder.created
            if c.server.name == "echo"
        ]
        assert all(c._shutdown_called for c in a_clients)
        assert not any(c._shutdown_called for c in b_clients)
        assert not any(c._shutdown_called for c in global_clients)
        await mgr.shutdown_all()
