"""Singleton-ish pool manager for MCP clients.

Holds one ``MCPClient`` per configured ``[mcp.<name>]`` server, keyed
by name. Clients are spawned **lazily** on the first call that needs
them (``list_methods`` or ``call_method``): configured-but-unused
servers cost nothing at startup.

Crash recovery: when a call fails with ``MCPTransportError``, the
manager shuts down the dead client, spawns a fresh one, and retries
the call exactly once. Repeated failures within the restart window
(default 3 restarts in 60 seconds) trip a circuit breaker — the
server is marked unhealthy, further calls raise
``UnhealthyServerError`` without spawning new clients until a
manual ``reset_health(name)``.

``list_methods`` results are cached per server with a short TTL
(default 60s) so callers (briefer, planner prompt builder) can hit
the method list repeatedly without re-querying the server.

``shutdown_all`` walks every active client, calling shutdown on each,
isolating per-client failures so one misbehaving server cannot block
clean daemon teardown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Deque

from kiso.mcp.client import MCPClient
from kiso.mcp.config import MCPServer
from kiso.mcp.http import MCPStreamableHTTPClient
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPError,
    MCPInvocationError,
    MCPMethod,
    MCPTransportError,
)
from kiso.mcp.stdio import MCPStdioClient

log = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_S = 60.0
_DEFAULT_RESTART_LIMIT = 3
_DEFAULT_RESTART_WINDOW_S = 60.0


class UnhealthyServerError(MCPError):
    """Raised when a call targets a server the manager has marked
    unhealthy (circuit breaker tripped). Recover via
    ``MCPManager.reset_health(server_name)``."""


def _default_factory(server: MCPServer) -> MCPClient:
    if server.transport == "stdio":
        return MCPStdioClient(server)
    if server.transport == "http":
        return MCPStreamableHTTPClient(server)
    raise ValueError(f"unknown transport: {server.transport!r}")


class MCPManager:
    def __init__(
        self,
        servers: dict[str, MCPServer],
        *,
        client_factory: Callable[[MCPServer], MCPClient] | None = None,
        cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
        restart_limit: int = _DEFAULT_RESTART_LIMIT,
        restart_window_s: float = _DEFAULT_RESTART_WINDOW_S,
    ) -> None:
        self._servers = servers
        self._factory = client_factory or _default_factory
        self._cache_ttl_s = cache_ttl_s
        self._restart_limit = restart_limit
        self._restart_window_s = restart_window_s
        self._pool: dict[str, MCPClient] = {}
        self._method_cache: dict[str, tuple[float, list[MCPMethod]]] = {}
        self._restart_times: dict[str, Deque[float]] = {}
        self._unhealthy: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def available_servers(self) -> list[str]:
        """Return names of configured AND enabled AND not-unhealthy servers."""
        return sorted(
            name
            for name, server in self._servers.items()
            if server.enabled and name not in self._unhealthy
        )

    def is_available(self, name: str) -> bool:
        server = self._servers.get(name)
        if server is None or not server.enabled:
            return False
        return name not in self._unhealthy

    async def list_methods(self, name: str) -> list[MCPMethod]:
        self._assert_known(name)
        cached = self._method_cache.get(name)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._cache_ttl_s:
            return cached[1]
        client = await self._get_or_spawn(name)
        methods = await client.list_methods()
        self._method_cache[name] = (now, methods)
        return methods

    async def call_method(
        self, name: str, method: str, args: dict
    ) -> MCPCallResult:
        self._assert_known(name)
        if name in self._unhealthy:
            raise UnhealthyServerError(
                f"mcp[{name}] marked unhealthy after {self._restart_limit} "
                f"consecutive failures; call MCPManager.reset_health() to retry"
            )

        client = await self._get_or_spawn(name)
        try:
            return await client.call_method(method, args)
        except MCPInvocationError:
            # Semantic error from the server (unknown method, bad args,
            # isError); not a transport failure — do not restart.
            raise
        except MCPTransportError as e:
            log.warning(
                "mcp[%s] transport error, attempting restart: %s", name, e
            )
            await self._record_failure(name)
            if name in self._unhealthy:
                # Tripped during _record_failure
                raise
            # Replace the dead client and retry exactly once
            await self._shutdown_client(name)
            client = await self._get_or_spawn(name, force=True)
            try:
                return await client.call_method(method, args)
            except MCPTransportError:
                await self._record_failure(name)
                raise

    async def cancel(self, name: str, request_id: Any) -> None:
        client = self._pool.get(name)
        if client is None:
            return
        try:
            await client.cancel(request_id)
        except Exception as e:  # noqa: BLE001
            log.debug("mcp[%s] cancel failed: %s", name, e)

    def invalidate_cache(self, name: str | None = None) -> None:
        if name is None:
            self._method_cache.clear()
        else:
            self._method_cache.pop(name, None)

    def reset_health(self, name: str) -> None:
        self._unhealthy.discard(name)
        self._restart_times.pop(name, None)

    async def shutdown_all(self) -> None:
        names = list(self._pool.keys())
        for name in names:
            try:
                await self._shutdown_client(name)
            except Exception as e:  # noqa: BLE001
                log.warning("mcp[%s] shutdown failed: %s", name, e)
        self._pool.clear()
        self._method_cache.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _assert_known(self, name: str) -> None:
        if name not in self._servers:
            raise KeyError(f"unknown mcp server: {name!r}")
        server = self._servers[name]
        if not server.enabled:
            raise ValueError(f"mcp server {name!r} is disabled in config")

    async def _get_or_spawn(
        self, name: str, *, force: bool = False
    ) -> MCPClient:
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            existing = self._pool.get(name)
            if existing is not None and existing.is_healthy() and not force:
                return existing
            if existing is not None:
                try:
                    await existing.shutdown()
                except Exception as e:  # noqa: BLE001
                    log.debug("mcp[%s] cleanup of stale client failed: %s", name, e)
                self._pool.pop(name, None)
                self._method_cache.pop(name, None)

            server = self._servers[name]
            client = self._factory(server)
            try:
                await client.initialize()
            except Exception:
                try:
                    await client.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                raise
            self._pool[name] = client
            return client

    async def _shutdown_client(self, name: str) -> None:
        client = self._pool.pop(name, None)
        self._method_cache.pop(name, None)
        if client is None:
            return
        try:
            await client.shutdown()
        except Exception as e:  # noqa: BLE001
            log.debug("mcp[%s] shutdown raised: %s", name, e)

    async def _record_failure(self, name: str) -> None:
        now = time.monotonic()
        dq = self._restart_times.setdefault(name, deque())
        dq.append(now)
        # Drop timestamps outside the window
        while dq and now - dq[0] > self._restart_window_s:
            dq.popleft()
        if len(dq) >= self._restart_limit:
            log.error(
                "mcp[%s] circuit breaker open: %d failures in %.0fs",
                name, len(dq), self._restart_window_s,
            )
            self._unhealthy.add(name)
            await self._shutdown_client(name)
