"""M1580 — In-process Mock MCP framework.

Generalist mocking layer for tests that need a populated MCP catalog
without spawning real subprocess servers. Composes on top of the
existing `FakeClient` pattern (see `tests/test_mcp_manager.py`) but
exposes a higher-level API: register fake MCPs by name + method
callbacks, then build an `MCPManager` wired to them. The catalog
becomes visible to the briefer through the same `MCPManager.list_methods`
path used in production — no monkey-patching of catalog read paths.

Usage:

    def test_search_routing(mock_mcp_catalog):
        mock_mcp_catalog.register("search-mcp", {
            "query": lambda **kw: {"hits": [{"url": "https://x.com"}]},
        })
        mgr = mock_mcp_catalog.build_manager()
        # ... feed mgr to the planner / worker pipeline ...
        mock_mcp_catalog.assert_called("search-mcp", "query")

The framework is deliberately blind to specific MCP names. Callers
register whatever names + methods their test needs, including names
that match production MCPs (e.g. `aider`, `playwright`) when they want
to exercise prompt-level routing without the real install.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kiso.mcp.config import MCPServer
from kiso.mcp.manager import MCPManager
from kiso.mcp.schemas import MCPCallResult, MCPMethod, MCPServerInfo


@dataclass
class MockMCPServer:
    """A single fake MCP server definition + its call log.

    M1613: ``descriptions`` lets a registration carry capability-flavoured
    text per method so the briefer surfaces "Search the web for a query"
    rather than the generic "mock method search". The planner uses the
    description to decide whether the MCP covers the user's intent
    (M1609 invariant).
    """

    name: str
    methods: dict[str, Callable[..., Any]]
    descriptions: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def to_server(self) -> MCPServer:
        return MCPServer(name=self.name, transport="stdio", command="mock")


class _MockClient:
    """Implements the subset of MCPClient that MCPManager invokes."""

    def __init__(self, mock: MockMCPServer) -> None:
        self._mock = mock
        self._initialized = False
        self._healthy = True

    async def initialize(self) -> MCPServerInfo:
        self._initialized = True
        return MCPServerInfo(
            name=self._mock.name, title=self._mock.name,
            version="0.0.0", protocol_version="2024-11-05",
            capabilities={}, instructions=None,
        )

    async def list_methods(self) -> list[MCPMethod]:
        return [
            MCPMethod(
                server=self._mock.name, name=method_name, title=None,
                description=self._mock.descriptions.get(
                    method_name, f"mock method {method_name}",
                ),
                input_schema={"type": "object"},
                output_schema=None, annotations=None,
            )
            for method_name in self._mock.methods
        ]

    async def list_resources(self) -> list:
        return []

    async def list_prompts(self) -> list:
        return []

    async def call_method(self, method: str, args: dict) -> MCPCallResult:
        cb = self._mock.methods.get(method)
        if cb is None:
            raise KeyError(
                f"mock {self._mock.name!r} has no method {method!r}; "
                f"registered: {sorted(self._mock.methods)}"
            )
        recorded_args = dict(args or {})
        self._mock.calls.append((method, recorded_args))
        result = cb(**recorded_args)
        structured = result if isinstance(result, dict) else None
        stdout = "" if structured is not None else str(result)
        return MCPCallResult(
            stdout_text=stdout, published_files=[],
            structured_content=structured, is_error=False,
        )

    async def cancel(self, request_id: Any) -> None:
        return None

    async def shutdown(self) -> None:
        self._initialized = False
        self._healthy = False

    def is_healthy(self) -> bool:
        return self._healthy and self._initialized


class MockMCPCatalog:
    """Test-side handle: register mocks + build a wired MCPManager.

    A fresh instance is provided per test by the `mock_mcp_catalog`
    fixture, so registrations cannot leak between tests.
    """

    def __init__(self) -> None:
        self.servers: dict[str, MockMCPServer] = {}

    def register(
        self,
        name: str,
        methods: dict[str, Callable[..., Any]],
        descriptions: dict[str, str] | None = None,
    ) -> MockMCPServer:
        srv = MockMCPServer(
            name=name,
            methods=dict(methods),
            descriptions=dict(descriptions or {}),
        )
        self.servers[name] = srv
        return srv

    def build_manager(self, **manager_kwargs: Any) -> MCPManager:
        servers_dict = {n: s.to_server() for n, s in self.servers.items()}
        clients = {n: _MockClient(s) for n, s in self.servers.items()}

        def factory(server: MCPServer, **_kw: Any) -> _MockClient:
            return clients[server.name]

        return MCPManager(
            servers_dict, client_factory=factory, **manager_kwargs,
        )

    def assert_called(
        self, name: str, method: str, args: dict | None = None,
    ) -> None:
        srv = self.servers.get(name)
        if srv is None:
            raise AssertionError(
                f"no mock registered with name {name!r}; "
                f"registered: {sorted(self.servers)}"
            )
        for m, a in srv.calls:
            if m != method:
                continue
            if args is None or a == args:
                return
        raise AssertionError(
            f"mock {name!r} method {method!r} was not called "
            f"(args={args!r}); recorded: {srv.calls}"
        )
