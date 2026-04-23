"""Pool manager for MCP clients.

Holds one ``MCPClient`` per ``(server_name, scope_key)``. Clients are
spawned **lazily** on the first call that needs them (``list_methods``
or ``call_method``): configured-but-unused servers cost nothing at
startup.

Scoping
-------
- A server whose config has no ``${session:*}`` tokens pools **globally**
  under ``scope_key = "_global"`` — every session shares one subprocess.
- A server whose config references ``${session:workspace}`` or
  ``${session:id}`` pools **per session** under
  ``scope_key = <session_id>``. Each session gets its own subprocess
  spawned with the tokens resolved against its own workspace.
- A call to a session-scoped server without ``session=...`` routes to
  the global pool entry (tokens stay unresolved), which is what the
  CLI ``kiso mcp test`` wants.

Crash recovery: when a call fails with ``MCPTransportError``, the
manager shuts down the dead client, spawns a fresh one, and retries
the call exactly once. Repeated failures within the restart window
trip a per-server circuit breaker — further calls raise
``UnhealthyServerError`` without spawning until a manual
``reset_health(name)``.

``list_methods`` results are cached per server (by name, not scope —
the exposed method list is a property of the server, not the session).

Eviction
--------
- **Idle** — a session-scoped client with no activity for
  ``session_idle_timeout_s`` seconds is shut down.
- **LRU** — if spawning a new session-scoped client would exceed
  ``max_session_clients_per_server`` for that server, the LRU entry
  is evicted first.

Global clients are never evicted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque

from kiso.config import KISO_DIR
from kiso.mcp.client import MCPClient
from kiso.mcp.config import MCPServer, resolve_session_tokens
from kiso.mcp.http import MCPStreamableHTTPClient
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPError,
    MCPInvocationError,
    MCPMethod,
    MCPPrompt,
    MCPPromptResult,
    MCPResource,
    MCPResourceContent,
    MCPTransportError,
)
from kiso.mcp.stdio import MCPStdioClient

log = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_S = 60.0
_DEFAULT_RESTART_LIMIT = 3
_DEFAULT_RESTART_WINDOW_S = 60.0
_DEFAULT_SESSION_IDLE_TIMEOUT_S = 1800.0
_DEFAULT_MAX_SESSION_CLIENTS_PER_SERVER = 32

GLOBAL_SCOPE = "_global"

# (server_name, scope_key, sandbox_uid). sandbox_uid is ``None`` for
# admin-context calls and for servers declared ``sandbox = "never"``;
# otherwise it pins the subprocess to a specific UID so user-role
# sessions cannot share a client with admin or another user.
PoolKey = tuple[str, str, int | None]


def _default_workspace_resolver(session_id: str) -> Path:
    return KISO_DIR / "sessions" / session_id


class UnhealthyServerError(MCPError):
    """Raised when a call targets a server the manager has marked
    unhealthy (circuit breaker tripped). Recover via
    ``MCPManager.reset_health(server_name)``."""


def _default_factory(
    server: MCPServer,
    *,
    extra_env: dict[str, str] | None = None,
    sandbox_uid: int | None = None,
    config: Any | None = None,
) -> MCPClient:
    if server.transport == "stdio":
        return MCPStdioClient(
            server, extra_env=extra_env, sandbox_uid=sandbox_uid,
            config=config,
        )
    if server.transport == "http":
        # HTTP transports have no subprocess — sandbox_uid is a no-op.
        return MCPStreamableHTTPClient(server, config=config)
    raise ValueError(f"unknown transport: {server.transport!r}")


class MCPManager:
    def __init__(
        self,
        servers: dict[str, MCPServer],
        *,
        client_factory: Callable[..., MCPClient] | None = None,
        config: Any | None = None,
        cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
        restart_limit: int = _DEFAULT_RESTART_LIMIT,
        restart_window_s: float = _DEFAULT_RESTART_WINDOW_S,
        session_idle_timeout_s: float = _DEFAULT_SESSION_IDLE_TIMEOUT_S,
        max_session_clients_per_server: int = _DEFAULT_MAX_SESSION_CLIENTS_PER_SERVER,
        workspace_resolver: Callable[[str], Path] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._servers = servers
        self._factory = client_factory or _default_factory
        self._config = config
        self._cache_ttl_s = cache_ttl_s
        self._restart_limit = restart_limit
        self._restart_window_s = restart_window_s
        self._session_idle_timeout_s = session_idle_timeout_s
        self._max_session_clients = max_session_clients_per_server
        self._workspace_resolver = (
            workspace_resolver or _default_workspace_resolver
        )
        self._clock = clock
        self._pool: dict[PoolKey, MCPClient] = {}
        self._last_used: dict[PoolKey, float] = {}
        self._method_cache: dict[str, tuple[float, list[MCPMethod]]] = {}
        self._resource_cache: dict[str, tuple[float, list[MCPResource]]] = {}
        self._prompt_cache: dict[str, tuple[float, list[MCPPrompt]]] = {}
        self._restart_times: dict[str, Deque[float]] = {}
        self._unhealthy: set[str] = set()
        self._locks: dict[PoolKey, asyncio.Lock] = {}
        self._session_env: dict[str, dict[str, str]] = {}
        self._eviction_task: asyncio.Task | None = None

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

    def set_session_env(self, session: str, env: dict[str, str]) -> None:
        """Register per-session env to inject into session-scoped spawns.

        Values set here reach a session-scoped **stdio** client the
        next time one is spawned for ``session``. A client that is
        already live keeps the env it was spawned with — callers who
        need fresh secrets must call :meth:`shutdown_session` first.

        HTTP transports have no subprocess env and ignore this hook;
        global-scope clients (stdio or HTTP) never receive it because
        the pool key isolates them.
        """
        self._session_env[session] = dict(env)

    async def list_methods(
        self,
        name: str,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ) -> list[MCPMethod]:
        self._assert_known(name)
        cached = self._method_cache.get(name)
        now = self._clock()
        if cached is not None and now - cached[0] < self._cache_ttl_s:
            return cached[1]
        client = await self._get_or_spawn(name, session, sandbox_uid)
        methods = await client.list_methods()
        self._method_cache[name] = (now, methods)
        return methods

    async def list_resources(
        self,
        name: str,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ) -> list[MCPResource]:
        self._assert_known(name)
        cached = self._resource_cache.get(name)
        now = self._clock()
        if cached is not None and now - cached[0] < self._cache_ttl_s:
            return cached[1]
        client = await self._get_or_spawn(name, session, sandbox_uid)
        resources = await client.list_resources()
        self._resource_cache[name] = (now, resources)
        return resources

    def list_resources_cached_only(self, name: str) -> list[MCPResource]:
        """Return cached resources for *name* without spawning the server."""
        if name not in self._servers:
            return []
        cached = self._resource_cache.get(name)
        if cached is None:
            return []
        ts, resources = cached
        if self._clock() - ts >= self._cache_ttl_s:
            return []
        return resources

    async def read_resource(
        self,
        name: str,
        uri: str,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ) -> list[MCPResourceContent]:
        self._assert_known(name)
        if name in self._unhealthy:
            raise UnhealthyServerError(
                f"mcp[{name}] marked unhealthy after {self._restart_limit} "
                f"consecutive failures; call MCPManager.reset_health() to retry"
            )
        client = await self._get_or_spawn(name, session, sandbox_uid)
        try:
            return await client.read_resource(uri)
        except MCPInvocationError:
            raise
        except MCPTransportError as e:
            log.warning(
                "mcp[%s] transport error on read_resource, restarting: %s", name, e
            )
            await self._record_failure(name)
            if name in self._unhealthy:
                raise
            key = self._scope_key(name, session, sandbox_uid)
            await self._shutdown_key(key)
            client = await self._get_or_spawn(
                name, session, sandbox_uid, force=True
            )
            try:
                return await client.read_resource(uri)
            except MCPTransportError:
                await self._record_failure(name)
                raise

    async def list_prompts(
        self,
        name: str,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ) -> list[MCPPrompt]:
        self._assert_known(name)
        cached = self._prompt_cache.get(name)
        now = self._clock()
        if cached is not None and now - cached[0] < self._cache_ttl_s:
            return cached[1]
        client = await self._get_or_spawn(name, session, sandbox_uid)
        prompts = await client.list_prompts()
        self._prompt_cache[name] = (now, prompts)
        return prompts

    def list_prompts_cached_only(self, name: str) -> list[MCPPrompt]:
        """Return cached prompts for *name* without spawning the server."""
        if name not in self._servers:
            return []
        cached = self._prompt_cache.get(name)
        if cached is None:
            return []
        ts, prompts = cached
        if self._clock() - ts >= self._cache_ttl_s:
            return []
        return prompts

    async def get_prompt(
        self,
        name: str,
        prompt_name: str,
        args: dict,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ) -> MCPPromptResult:
        self._assert_known(name)
        if name in self._unhealthy:
            raise UnhealthyServerError(
                f"mcp[{name}] marked unhealthy after {self._restart_limit} "
                f"consecutive failures; call MCPManager.reset_health() to retry"
            )
        client = await self._get_or_spawn(name, session, sandbox_uid)
        try:
            return await client.get_prompt(prompt_name, args)
        except MCPInvocationError:
            raise
        except MCPTransportError as e:
            log.warning(
                "mcp[%s] transport error on get_prompt, restarting: %s", name, e
            )
            await self._record_failure(name)
            if name in self._unhealthy:
                raise
            key = self._scope_key(name, session, sandbox_uid)
            await self._shutdown_key(key)
            client = await self._get_or_spawn(
                name, session, sandbox_uid, force=True
            )
            try:
                return await client.get_prompt(prompt_name, args)
            except MCPTransportError:
                await self._record_failure(name)
                raise

    def list_methods_cached_only(self, name: str) -> list[MCPMethod]:
        """Return cached methods for *name* without spawning the server."""
        if name not in self._servers:
            return []
        cached = self._method_cache.get(name)
        if cached is None:
            return []
        ts, methods = cached
        if self._clock() - ts >= self._cache_ttl_s:
            return []
        return methods

    async def call_method(
        self,
        name: str,
        method: str,
        args: dict,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ) -> MCPCallResult:
        self._assert_known(name)
        if name in self._unhealthy:
            raise UnhealthyServerError(
                f"mcp[{name}] marked unhealthy after {self._restart_limit} "
                f"consecutive failures; call MCPManager.reset_health() to retry"
            )

        client = await self._get_or_spawn(name, session, sandbox_uid)
        try:
            return await client.call_method(method, args)
        except MCPInvocationError:
            raise
        except MCPTransportError as e:
            log.warning(
                "mcp[%s] transport error, attempting restart: %s", name, e
            )
            await self._record_failure(name)
            if name in self._unhealthy:
                raise
            key = self._scope_key(name, session, sandbox_uid)
            await self._shutdown_key(key)
            client = await self._get_or_spawn(
                name, session, sandbox_uid, force=True
            )
            try:
                return await client.call_method(method, args)
            except MCPTransportError:
                await self._record_failure(name)
                raise

    async def cancel(
        self,
        name: str,
        request_id: Any,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ) -> None:
        key = self._scope_key(name, session, sandbox_uid)
        client = self._pool.get(key)
        if client is None:
            return
        try:
            await client.cancel(request_id)
        except Exception as e:  # noqa: BLE001
            log.debug("mcp[%s] cancel failed: %s", name, e)

    def invalidate_cache(self, name: str | None = None) -> None:
        if name is None:
            self._method_cache.clear()
            self._resource_cache.clear()
            self._prompt_cache.clear()
        else:
            self._method_cache.pop(name, None)
            self._resource_cache.pop(name, None)
            self._prompt_cache.pop(name, None)

    def reset_health(self, name: str) -> None:
        self._unhealthy.discard(name)
        self._restart_times.pop(name, None)

    async def shutdown_all(self) -> None:
        if self._eviction_task is not None:
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._eviction_task = None
        keys = list(self._pool.keys())
        for key in keys:
            try:
                await self._shutdown_key(key)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "mcp[%s:%s:%s] shutdown failed: %s",
                    key[0], key[1], key[2], e,
                )
        self._pool.clear()
        self._method_cache.clear()
        self._resource_cache.clear()
        self._prompt_cache.clear()
        self._last_used.clear()

    async def shutdown_session(self, session: str) -> None:
        """Shut down every pool entry scoped to *session*."""
        keys = [k for k in list(self._pool.keys()) if k[1] == session]
        for key in keys:
            await self._shutdown_key(key)
        self._session_env.pop(session, None)

    def start_eviction_loop(self, interval_s: float = 60.0) -> None:
        """Start the background idle-eviction task.

        Callers typically invoke this from the daemon startup path. The
        loop wakes every *interval_s* seconds and shuts down any
        session-scoped client unused for more than
        ``session_idle_timeout_s``.
        """
        if self._eviction_task is not None:
            return
        self._eviction_task = asyncio.create_task(
            self._eviction_loop(interval_s), name="mcp-idle-eviction"
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _assert_known(self, name: str) -> None:
        if name not in self._servers:
            raise KeyError(f"unknown mcp server: {name!r}")
        server = self._servers[name]
        if not server.enabled:
            raise ValueError(f"mcp server {name!r} is disabled in config")

    def _scope_key(
        self, name: str, session: str | None, sandbox_uid: int | None
    ) -> PoolKey:
        server = self._servers[name]
        # A server with sandbox="never" opts out of role-based UID
        # isolation — admin and user calls share one client.
        uid_key = None if server.sandbox == "never" else sandbox_uid
        if session is None:
            return (name, GLOBAL_SCOPE, uid_key)
        if not server.is_session_scoped:
            return (name, GLOBAL_SCOPE, uid_key)
        return (name, session, uid_key)

    async def _get_or_spawn(
        self,
        name: str,
        session: str | None,
        sandbox_uid: int | None = None,
        *,
        force: bool = False,
    ) -> MCPClient:
        key = self._scope_key(name, session, sandbox_uid)
        is_session_scope = key[1] != GLOBAL_SCOPE
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._pool.get(key)
            if existing is not None and existing.is_healthy() and not force:
                if is_session_scope:
                    self._last_used[key] = self._clock()
                return existing
            if existing is not None:
                try:
                    await existing.shutdown()
                except Exception as e:  # noqa: BLE001
                    log.debug(
                        "mcp[%s:%s:%s] stale cleanup failed: %s",
                        key[0], key[1], key[2], e,
                    )
                self._pool.pop(key, None)
                self._last_used.pop(key, None)

            server = self._servers[name]
            extra_env: dict[str, str] | None = None
            if is_session_scope:
                workspace = self._workspace_resolver(session)  # type: ignore[arg-type]
                server = resolve_session_tokens(server, session, workspace)  # type: ignore[arg-type]
                await self._evict_to_bound(name, exclude_key=key)
                extra_env = self._session_env.get(session, {})  # type: ignore[arg-type]

            try:
                client = self._factory(
                    server, extra_env=extra_env, sandbox_uid=key[2],
                    config=self._config,
                )
            except TypeError:
                # Test factories predating the ``config=`` kwarg: fall
                # back to the older signature so the existing fakes
                # keep working.
                client = self._factory(
                    server, extra_env=extra_env, sandbox_uid=key[2],
                )
            try:
                await client.initialize()
            except Exception:
                try:
                    await client.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                raise
            self._pool[key] = client
            if is_session_scope:
                self._last_used[key] = self._clock()
            return client

    async def _shutdown_key(self, key: PoolKey) -> None:
        client = self._pool.pop(key, None)
        self._last_used.pop(key, None)
        self._locks.pop(key, None)
        if key[1] == GLOBAL_SCOPE:
            self._method_cache.pop(key[0], None)
            self._resource_cache.pop(key[0], None)
            self._prompt_cache.pop(key[0], None)
        else:
            self._maybe_prune_session(key[1])
        if client is None:
            return
        try:
            await client.shutdown()
        except Exception as e:  # noqa: BLE001
            log.debug(
                "mcp[%s:%s:%s] shutdown raised: %s",
                key[0], key[1], key[2], e,
            )

    def _maybe_prune_session(self, session: str) -> None:
        if any(k[1] == session for k in self._pool):
            return
        self._session_env.pop(session, None)

    async def _record_failure(self, name: str) -> None:
        now = self._clock()
        dq = self._restart_times.setdefault(name, deque())
        dq.append(now)
        while dq and now - dq[0] > self._restart_window_s:
            dq.popleft()
        if len(dq) >= self._restart_limit:
            log.error(
                "mcp[%s] circuit breaker open: %d failures in %.0fs",
                name, len(dq), self._restart_window_s,
            )
            self._unhealthy.add(name)
            self._method_cache.pop(name, None)
            self._resource_cache.pop(name, None)
            self._prompt_cache.pop(name, None)
            for key in [k for k in list(self._pool.keys()) if k[0] == name]:
                await self._shutdown_key(key)

    async def _evict_idle_now(self) -> None:
        """Shut down every session-scoped client idle past the bound."""
        now = self._clock()
        stale = [
            k for k, t in list(self._last_used.items())
            if k[1] != GLOBAL_SCOPE
            and now - t >= self._session_idle_timeout_s
        ]
        for key in stale:
            await self._shutdown_key(key)

    async def _evict_to_bound(
        self, name: str, *, exclude_key: PoolKey
    ) -> None:
        """Before adding a session client, LRU-prune to stay at bound."""
        session_keys = [
            k for k in self._pool.keys()
            if k[0] == name and k[1] != GLOBAL_SCOPE and k != exclude_key
        ]
        if len(session_keys) < self._max_session_clients:
            return
        session_keys.sort(key=lambda k: self._last_used.get(k, 0.0))
        to_drop = len(session_keys) - self._max_session_clients + 1
        for key in session_keys[:to_drop]:
            await self._shutdown_key(key)

    async def _eviction_loop(self, interval_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self._evict_idle_now()
                except Exception as e:  # noqa: BLE001
                    log.warning("mcp eviction pass failed: %s", e)
        except asyncio.CancelledError:
            pass
