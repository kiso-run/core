"""Unit tests for ``MCPManager``: lazy start, pool, restart, cache."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.manager import MCPManager, UnhealthyServerError
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPInvocationError,
    MCPMethod,
    MCPPrompt,
    MCPPromptArgument,
    MCPPromptMessage,
    MCPPromptResult,
    MCPResource,
    MCPResourceContent,
    MCPServerInfo,
    MCPTransportError,
)


def _server(name: str = "s1", transport: str = "stdio") -> MCPServer:
    return MCPServer(
        name=name,
        transport=transport,
        command="dummy" if transport == "stdio" else None,
        url="http://dummy" if transport == "http" else None,
    )


def _info(name: str = "s1") -> MCPServerInfo:
    return MCPServerInfo(
        name=name,
        title=None,
        version="1.0",
        protocol_version="2025-06-18",
        capabilities={},
        instructions=None,
    )


def _method(name: str, server: str = "s1") -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description="",
        input_schema={"type": "object"},
        output_schema=None,
        annotations=None,
    )


def _resource(uri: str, server: str = "s1") -> MCPResource:
    return MCPResource(
        server=server,
        uri=uri,
        name=uri.rsplit("/", 1)[-1] or uri,
        description="",
        mime_type="text/plain",
    )


def _prompt(name: str, server: str = "s1") -> MCPPrompt:
    return MCPPrompt(
        server=server,
        name=name,
        description="",
        arguments=[
            MCPPromptArgument(name="x", description="", required=True),
        ],
    )


class FakeClient:
    """Minimal MCPClient stand-in for manager tests. Records calls."""

    def __init__(
        self,
        server: MCPServer,
        *,
        methods: list[MCPMethod] | None = None,
        resources: list[MCPResource] | None = None,
        prompts: list[MCPPrompt] | None = None,
    ) -> None:
        self.server = server
        self._methods = methods or [_method("echo", server.name)]
        self._resources = (
            resources
            if resources is not None
            else [_resource("kiso://r/1", server.name)]
        )
        self._prompts = (
            prompts
            if prompts is not None
            else [_prompt("p1", server.name)]
        )
        self._initialized = False
        self._healthy = True
        self._call_count = 0
        self._crash_next_call = False
        self._shutdown_called = False
        self._cancelled: list[Any] = []
        self._list_call_count = 0
        self._list_resources_call_count = 0
        self._read_resource_calls: list[str] = []
        self._list_prompts_call_count = 0
        self._get_prompt_calls: list[tuple[str, dict]] = []

    async def initialize(self) -> MCPServerInfo:
        self._initialized = True
        return _info(self.server.name)

    async def list_methods(self) -> list[MCPMethod]:
        self._list_call_count += 1
        return list(self._methods)

    async def list_resources(self) -> list[MCPResource]:
        self._list_resources_call_count += 1
        return list(self._resources)

    async def read_resource(self, uri: str) -> list[MCPResourceContent]:
        self._read_resource_calls.append(uri)
        return [
            MCPResourceContent(
                uri=uri, mime_type="text/plain",
                text=f"body:{uri}", blob=None,
            ),
        ]

    async def list_prompts(self) -> list[MCPPrompt]:
        self._list_prompts_call_count += 1
        return list(self._prompts)

    async def get_prompt(self, name: str, args: dict) -> MCPPromptResult:
        self._get_prompt_calls.append((name, dict(args or {})))
        return MCPPromptResult(
            description=f"rendered:{name}",
            messages=[
                MCPPromptMessage(
                    role="user",
                    text=f"{name}({args or {}})",
                ),
            ],
        )

    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        self._call_count += 1
        if self._crash_next_call:
            self._crash_next_call = False
            self._healthy = False
            raise MCPTransportError("simulated crash")
        return MCPCallResult(
            stdout_text=f"called {name}",
            published_files=[],
            structured_content=None,
            is_error=False,
        )

    async def cancel(self, request_id: Any) -> None:
        self._cancelled.append(request_id)

    async def shutdown(self) -> None:
        self._shutdown_called = True
        self._initialized = False
        self._healthy = False

    def is_healthy(self) -> bool:
        return self._healthy and self._initialized


@pytest.fixture
def fake_factory():
    """Returns a factory that records all client instances it creates."""
    created: list[FakeClient] = []

    def factory(
        server: MCPServer,
        *,
        extra_env: dict | None = None,
        sandbox_uid: int | None = None,
    ) -> FakeClient:
        c = FakeClient(server)
        created.append(c)
        return c

    factory.created = created  # type: ignore[attr-defined]
    return factory


# ---------------------------------------------------------------------------
# Lazy start + pool
# ---------------------------------------------------------------------------


class TestLazyStart:
    async def test_no_client_before_use(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        assert fake_factory.created == []

    async def test_first_list_methods_spawns_client(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        methods = await mgr.list_methods("s1")
        assert len(fake_factory.created) == 1
        assert fake_factory.created[0]._initialized is True
        assert [m.name for m in methods] == ["echo"]
        await mgr.shutdown_all()

    async def test_second_list_methods_reuses_client(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_methods("s1")
        await mgr.list_methods("s1")
        assert len(fake_factory.created) == 1
        await mgr.shutdown_all()

    async def test_unknown_server_raises(self, fake_factory):
        mgr = MCPManager({}, client_factory=fake_factory)
        with pytest.raises(KeyError, match="unknown"):
            await mgr.list_methods("unknown")

    async def test_disabled_server_raises(self, fake_factory):
        server = MCPServer(
            name="s1", transport="stdio", command="dummy", enabled=False
        )
        mgr = MCPManager({"s1": server}, client_factory=fake_factory)
        with pytest.raises(ValueError, match="disabled"):
            await mgr.list_methods("s1")


# ---------------------------------------------------------------------------
# list_methods cache
# ---------------------------------------------------------------------------


class TestListMethodsCache:
    async def test_cache_hit(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_methods("s1")
        await mgr.list_methods("s1")
        # Cache serves the second call without hitting the client
        assert fake_factory.created[0]._list_call_count == 1
        await mgr.shutdown_all()

    async def test_cache_invalidation(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_methods("s1")
        mgr.invalidate_cache("s1")
        await mgr.list_methods("s1")
        assert fake_factory.created[0]._list_call_count == 2
        await mgr.shutdown_all()


class TestListMethodsCachedOnly:
    """`list_methods_cached_only` never spawns a server.

    Used by `kiso.brain.common.format_mcp_catalog` to feed the
    briefer's MCP catalog without paying transport setup costs.
    """

    async def test_unknown_server_returns_empty(self, fake_factory):
        mgr = MCPManager({}, client_factory=fake_factory)
        assert mgr.list_methods_cached_only("ghost") == []

    async def test_never_queried_returns_empty(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        # No call to list_methods → cache is empty
        assert mgr.list_methods_cached_only("s1") == []
        # And the client was never created
        assert len(fake_factory.created) == 0

    async def test_returns_cached_methods_after_warm(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_methods("s1")  # warm cache
        cached = mgr.list_methods_cached_only("s1")
        assert len(cached) == 1
        assert cached[0].name == "echo"
        # Calling cached_only does NOT increment the spawn count
        assert fake_factory.created[0]._list_call_count == 1
        await mgr.shutdown_all()


# ---------------------------------------------------------------------------
# Resources: list + read + cache
# ---------------------------------------------------------------------------


class TestListResources:
    async def test_first_call_queries_client(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        resources = await mgr.list_resources("s1")
        assert [r.uri for r in resources] == ["kiso://r/1"]
        assert fake_factory.created[0]._list_resources_call_count == 1
        await mgr.shutdown_all()

    async def test_cache_hit_within_ttl(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_resources("s1")
        await mgr.list_resources("s1")
        assert fake_factory.created[0]._list_resources_call_count == 1
        await mgr.shutdown_all()

    async def test_invalidate_cache_forces_reload(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_resources("s1")
        mgr.invalidate_cache("s1")
        await mgr.list_resources("s1")
        assert fake_factory.created[0]._list_resources_call_count == 2
        await mgr.shutdown_all()


class TestListResourcesCachedOnly:
    async def test_unknown_server_returns_empty(self, fake_factory):
        mgr = MCPManager({}, client_factory=fake_factory)
        assert mgr.list_resources_cached_only("ghost") == []

    async def test_never_queried_returns_empty(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        assert mgr.list_resources_cached_only("s1") == []
        assert len(fake_factory.created) == 0

    async def test_returns_cached_after_warm(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_resources("s1")
        cached = mgr.list_resources_cached_only("s1")
        assert [r.uri for r in cached] == ["kiso://r/1"]
        assert fake_factory.created[0]._list_resources_call_count == 1
        await mgr.shutdown_all()


class TestReadResource:
    async def test_read_dispatches_to_client(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        blocks = await mgr.read_resource("s1", "kiso://r/1")
        assert len(blocks) == 1
        assert blocks[0].text == "body:kiso://r/1"
        assert fake_factory.created[0]._read_resource_calls == ["kiso://r/1"]
        await mgr.shutdown_all()

    async def test_unknown_server_raises_keyerror(self, fake_factory):
        mgr = MCPManager({}, client_factory=fake_factory)
        with pytest.raises(KeyError):
            await mgr.read_resource("ghost", "kiso://x")


# ---------------------------------------------------------------------------
# Prompts: list + get + cache
# ---------------------------------------------------------------------------


class TestListPrompts:
    async def test_first_call_queries_client(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        prompts = await mgr.list_prompts("s1")
        assert [p.name for p in prompts] == ["p1"]
        assert fake_factory.created[0]._list_prompts_call_count == 1
        await mgr.shutdown_all()

    async def test_cache_hit_within_ttl(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_prompts("s1")
        await mgr.list_prompts("s1")
        assert fake_factory.created[0]._list_prompts_call_count == 1
        await mgr.shutdown_all()

    async def test_invalidate_cache_forces_reload(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_prompts("s1")
        mgr.invalidate_cache("s1")
        await mgr.list_prompts("s1")
        assert fake_factory.created[0]._list_prompts_call_count == 2
        await mgr.shutdown_all()


class TestListPromptsCachedOnly:
    async def test_unknown_server_returns_empty(self, fake_factory):
        mgr = MCPManager({}, client_factory=fake_factory)
        assert mgr.list_prompts_cached_only("ghost") == []

    async def test_never_queried_returns_empty(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        assert mgr.list_prompts_cached_only("s1") == []
        assert len(fake_factory.created) == 0

    async def test_returns_cached_after_warm(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_prompts("s1")
        cached = mgr.list_prompts_cached_only("s1")
        assert [p.name for p in cached] == ["p1"]
        assert fake_factory.created[0]._list_prompts_call_count == 1
        await mgr.shutdown_all()


class TestGetPrompt:
    async def test_get_dispatches_to_client(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        rendered = await mgr.get_prompt("s1", "p1", {"x": "y"})
        assert rendered.description == "rendered:p1"
        assert fake_factory.created[0]._get_prompt_calls == [("p1", {"x": "y"})]
        await mgr.shutdown_all()

    async def test_unknown_server_raises_keyerror(self, fake_factory):
        mgr = MCPManager({}, client_factory=fake_factory)
        with pytest.raises(KeyError):
            await mgr.get_prompt("ghost", "p1", {})


# ---------------------------------------------------------------------------
# call_method: timeout + cancellation
# ---------------------------------------------------------------------------


class TestCallMethod:
    async def test_happy_call(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        result = await mgr.call_method("s1", "echo", {"text": "hi"})
        assert result.is_error is False
        assert "echo" in result.stdout_text
        await mgr.shutdown_all()

    async def test_call_invocation_error_propagates(self, fake_factory):
        """Invocation errors (isError from server, unknown method) are
        raised to the caller without triggering a restart."""
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        await mgr.list_methods("s1")

        async def _raising_call(name: str, args: dict):
            raise MCPInvocationError("nope")

        fake_factory.created[0].call_method = _raising_call  # type: ignore[method-assign]
        with pytest.raises(MCPInvocationError):
            await mgr.call_method("s1", "echo", {})
        # Not a restart situation
        assert len(fake_factory.created) == 1
        await mgr.shutdown_all()


# ---------------------------------------------------------------------------
# Crash recovery + circuit breaker
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    async def test_restart_after_transport_error(self, fake_factory):
        """Transport error → mark client unhealthy → next call spawns a
        fresh client → retry succeeds."""
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        # Prime the pool
        await mgr.list_methods("s1")
        fake_factory.created[0]._crash_next_call = True

        # First call crashes; manager catches, restarts, retries (succeeds)
        result = await mgr.call_method("s1", "echo", {})
        assert result.is_error is False
        # A second client was created for the restart
        assert len(fake_factory.created) == 2

        await mgr.shutdown_all()

    async def test_circuit_breaker_trips_after_repeated_failures(
        self, fake_factory
    ):
        """Consecutive transport failures within the window → server
        marked unhealthy, further calls raise UnhealthyServerError
        without spawning new clients. One call_method produces at most
        two transport failures (first attempt + one retry), so
        restart_limit=2 trips the breaker in a single call."""
        mgr = MCPManager(
            {"s1": _server("s1")},
            client_factory=fake_factory,
            restart_limit=2,
            restart_window_s=60.0,
        )

        async def _always_crash(name: str, args: dict):
            raise MCPTransportError("crash")

        # Force 3 crashes in a row by making every new client crash on
        # its first call. The manager will spawn, crash, retry, crash,
        # retry, crash. After 3 crashes → circuit open.
        # Patch the factory to build crash-on-call clients.
        def crashy_factory(server, *, extra_env=None, sandbox_uid=None):
            c = FakeClient(server)
            c.call_method = _always_crash  # type: ignore[method-assign]
            fake_factory.created.append(c)
            return c

        mgr._factory = crashy_factory  # type: ignore[assignment]

        with pytest.raises(MCPTransportError):
            await mgr.call_method("s1", "echo", {})
        assert mgr.is_available("s1") is False

        with pytest.raises(UnhealthyServerError):
            await mgr.call_method("s1", "echo", {})

        await mgr.shutdown_all()


# ---------------------------------------------------------------------------
# shutdown_all
# ---------------------------------------------------------------------------


class TestShutdownAll:
    async def test_shuts_down_every_active_client(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1"), "s2": _server("s2")},
            client_factory=fake_factory,
        )
        await mgr.list_methods("s1")
        await mgr.list_methods("s2")
        await mgr.shutdown_all()
        assert all(c._shutdown_called for c in fake_factory.created)

    async def test_shutdown_all_continues_through_failures(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1"), "s2": _server("s2")},
            client_factory=fake_factory,
        )
        await mgr.list_methods("s1")
        await mgr.list_methods("s2")

        async def _raise(self):
            raise RuntimeError("teardown failure")

        # First client raises during shutdown; second must still run
        fake_factory.created[0].shutdown = _raise.__get__(  # type: ignore[method-assign]
            fake_factory.created[0]
        )
        await mgr.shutdown_all()
        assert fake_factory.created[1]._shutdown_called is True

    async def test_shutdown_all_without_activity(self, fake_factory):
        mgr = MCPManager(
            {"s1": _server("s1")}, client_factory=fake_factory
        )
        # No methods were ever called — nothing to shut down
        await mgr.shutdown_all()
        assert fake_factory.created == []


# ---------------------------------------------------------------------------
# available_servers / is_available
# ---------------------------------------------------------------------------


class TestAvailability:
    async def test_available_servers_lists_enabled(self, fake_factory):
        mgr = MCPManager(
            {
                "s1": _server("s1"),
                "s2": MCPServer(
                    name="s2", transport="stdio", command="x", enabled=False
                ),
            },
            client_factory=fake_factory,
        )
        assert set(mgr.available_servers()) == {"s1"}
