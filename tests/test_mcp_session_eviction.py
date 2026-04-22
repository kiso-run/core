"""Tests for idle + LRU eviction of per-session MCP clients.

Business requirement: the per-session client pool cannot grow without
bound. Two evictions keep it in check:

- **Idle eviction** — a session-scoped client unused for
  ``mcp_session_idle_timeout`` seconds is gracefully shut down.
- **LRU eviction** — when adding a new session-scoped client would
  push the per-server count above ``mcp_max_session_clients_per_server``,
  the least-recently-used session client for that server is shut
  down before the new one spawns.

Global-scope clients are never evicted (there is only ever one per
server, and the daemon needs it alive for config-driven liveness).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiso.mcp.config import MCPServer, parse_mcp_section
from kiso.mcp.manager import MCPManager
from kiso.mcp.schemas import MCPCallResult, MCPMethod, MCPServerInfo


def _info(name: str = "s") -> MCPServerInfo:
    return MCPServerInfo(
        name=name,
        title=None,
        version="1.0",
        protocol_version="2025-06-18",
        capabilities={},
        instructions=None,
    )


class RecordingClient:
    def __init__(self, server: MCPServer, *, extra_env: dict[str, str] | None = None) -> None:
        self.server = server
        self._healthy = True
        self._initialized = False
        self._shutdown_called = False

    async def initialize(self) -> MCPServerInfo:
        self._initialized = True
        return _info(self.server.name)

    async def list_methods(self) -> list[MCPMethod]:
        return []

    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        return MCPCallResult(
            stdout_text="",
            published_files=[],
            structured_content=None,
            is_error=False,
        )

    async def cancel(self, request_id: Any) -> None:
        pass

    async def shutdown(self) -> None:
        self._shutdown_called = True
        self._healthy = False
        self._initialized = False

    def is_healthy(self) -> bool:
        return self._healthy and self._initialized


@pytest.fixture
def recorder():
    created: list[RecordingClient] = []

    def factory(server: MCPServer, *, extra_env: dict[str, str] | None = None) -> RecordingClient:
        c = RecordingClient(server, extra_env=extra_env)
        created.append(c)
        return c

    factory.created = created  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def workspace_resolver(tmp_path):
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
            }
        }
    )[name]


def _plain_server(name: str = "echo") -> MCPServer:
    return parse_mcp_section(
        {
            name: {"transport": "stdio", "command": "echo-server"}
        }
    )[name]


class MockClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestIdleEviction:
    async def test_idle_session_client_is_shut_down(
        self, recorder, workspace_resolver
    ):
        clock = MockClock()
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            session_idle_timeout_s=30.0,
            clock=clock,
        )
        await mgr.call_method("fs", "read", {}, session="A")
        client_a = recorder.created[0]
        assert not client_a._shutdown_called

        # Advance past the idle window.
        clock.advance(31.0)
        await mgr._evict_idle_now()

        assert client_a._shutdown_called

    async def test_recent_session_client_is_kept(
        self, recorder, workspace_resolver
    ):
        clock = MockClock()
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            session_idle_timeout_s=30.0,
            clock=clock,
        )
        await mgr.call_method("fs", "read", {}, session="A")
        client_a = recorder.created[0]

        clock.advance(10.0)
        await mgr._evict_idle_now()

        assert not client_a._shutdown_called

    async def test_global_clients_never_idle_evicted(
        self, recorder, workspace_resolver
    ):
        clock = MockClock()
        mgr = MCPManager(
            {"echo": _plain_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            session_idle_timeout_s=30.0,
            clock=clock,
        )
        await mgr.call_method("echo", "echo", {}, session="A")
        echo_client = recorder.created[0]

        clock.advance(10_000.0)
        await mgr._evict_idle_now()

        assert not echo_client._shutdown_called

    async def test_activity_resets_idle_timer(
        self, recorder, workspace_resolver
    ):
        clock = MockClock()
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            session_idle_timeout_s=30.0,
            clock=clock,
        )
        await mgr.call_method("fs", "read", {}, session="A")
        client_a = recorder.created[0]

        # Move forward but keep the client active via another call.
        clock.advance(20.0)
        await mgr.call_method("fs", "read", {}, session="A")
        clock.advance(20.0)
        await mgr._evict_idle_now()

        # 20s since last activity is still under the 30s window.
        assert not client_a._shutdown_called


class TestSessionStateCleanup:
    """When the last client for a session is evicted or shut down, the
    manager drops that session's bookkeeping (env + locks) so long-lived
    daemons don't accumulate stale per-session state."""

    async def test_idle_eviction_prunes_session_env(
        self, recorder, workspace_resolver
    ):
        clock = MockClock()
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            session_idle_timeout_s=30.0,
            clock=clock,
        )
        mgr.set_session_env("A", {"SECRET": "alpha"})
        await mgr.call_method("fs", "read", {}, session="A")
        assert "A" in mgr._session_env

        clock.advance(31.0)
        await mgr._evict_idle_now()

        assert "A" not in mgr._session_env

    async def test_lru_eviction_prunes_session_env(
        self, recorder, workspace_resolver
    ):
        clock = MockClock()
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            max_session_clients_per_server=1,
            clock=clock,
        )
        mgr.set_session_env("A", {"SECRET": "alpha"})
        mgr.set_session_env("B", {"SECRET": "beta"})
        await mgr.call_method("fs", "read", {}, session="A")
        clock.advance(1.0)
        await mgr.call_method("fs", "read", {}, session="B")
        # A was LRU-evicted; its env must be gone too.
        assert "A" not in mgr._session_env
        assert "B" in mgr._session_env
        await mgr.shutdown_all()


class TestLRUEviction:
    async def test_lru_eviction_at_pool_bound(
        self, recorder, workspace_resolver
    ):
        clock = MockClock()
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            max_session_clients_per_server=2,
            clock=clock,
        )
        # Spawn 3 session clients for the same server (A, B, C).
        # A is the oldest; B second; C triggers eviction of A.
        await mgr.call_method("fs", "read", {}, session="A")
        clock.advance(1.0)
        await mgr.call_method("fs", "read", {}, session="B")
        clock.advance(1.0)
        await mgr.call_method("fs", "read", {}, session="C")

        client_a = recorder.created[0]
        client_b = recorder.created[1]
        client_c = recorder.created[2]

        assert client_a._shutdown_called, "A should be LRU-evicted"
        assert not client_b._shutdown_called
        assert not client_c._shutdown_called
        await mgr.shutdown_all()

    async def test_lru_is_per_server(self, recorder, workspace_resolver):
        """Two session-scoped servers each have their own LRU bound."""
        servers = {
            "fs": _session_server("fs"),
            "db": _session_server("db"),
        }
        clock = MockClock()
        mgr = MCPManager(
            servers,
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            max_session_clients_per_server=2,
            clock=clock,
        )
        await mgr.call_method("fs", "read", {}, session="A")
        await mgr.call_method("fs", "read", {}, session="B")
        await mgr.call_method("db", "read", {}, session="A")
        await mgr.call_method("db", "read", {}, session="B")
        # 4 clients, none evicted — each server is at its own bound.
        assert sum(not c._shutdown_called for c in recorder.created) == 4
        await mgr.shutdown_all()
