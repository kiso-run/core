"""Background pre-loader for the MCP method catalog.

``MCPManager.list_methods_cached_only`` is empty until someone calls
``list_methods`` — which means the first user message after a daemon
boot sees a cold catalog and the planner may route via exec instead
of the MCP method that would otherwise be available.

``warm_catalog(manager)`` pre-loads the catalog in the background,
bounded by concurrency and a total wall-clock deadline. Per-server
failures are isolated (logged + skipped). Callers fire it with
``asyncio.create_task`` during daemon boot — they do NOT await it.

Session-scoped servers (whose ``args``/``cwd``/``env`` reference
``${session:workspace}`` or ``${session:id}``) cannot be spawned
with ``session=None`` — the tokens never get substituted, the
process launches against literal placeholder paths, and either
fails to start or returns no tools. For those, warmup passes a
synthetic ``__kiso_warmup__`` session id and pre-creates the
corresponding workspace directory so the tokens resolve to a real
path. Method/tool lists are server-level (independent of session
state), so caching them under any valid session is correct.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


log = logging.getLogger(__name__)


_WARMUP_SESSION_ID = "__kiso_warmup__"


def _prepare_warmup_session(manager: Any) -> str | None:
    """Create the warmup workspace dir (and its `pub/` subdir) so
    `${session:workspace}` resolves to a real, writable path when the
    warmup spawns a session-scoped server.

    Returns the warmup session id on success, None when the manager
    doesn't expose `workspace_for` (older test stubs / fakes — fall
    back to `session=None` warmup as before).
    """
    workspace_for = getattr(manager, "workspace_for", None)
    if workspace_for is None:
        return None
    try:
        ws = workspace_for(_WARMUP_SESSION_ID)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "pub").mkdir(exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mcp warmup: could not prepare warmup workspace at %s: %s",
            workspace_for(_WARMUP_SESSION_ID) if workspace_for else "?",
            exc,
        )
        return None
    return _WARMUP_SESSION_ID


async def warm_catalog(
    manager: Any,
    *,
    concurrency: int = 3,
    deadline_s: float = 10.0,
) -> None:
    """Pre-load the MCP method catalog for every enabled server.

    Arguments are keyword-only to keep the call site readable from the
    daemon boot path.
    """
    if manager is None:
        return
    try:
        servers = manager.available_servers()
    except Exception as exc:  # noqa: BLE001
        log.warning("mcp warmup: available_servers() raised: %s", exc)
        return
    if not servers:
        return

    warmup_session_id = _prepare_warmup_session(manager)
    is_session_scoped = getattr(manager, "is_session_scoped", lambda _n: False)

    def _session_for(name: str) -> str | None:
        """Pick the session id to pass to manager calls for *name*:
        warmup-session for session-scoped servers (so
        `${session:workspace}` resolves), None for global servers
        (preserves existing pool behaviour)."""
        if warmup_session_id is None:
            return None
        try:
            return warmup_session_id if is_session_scoped(name) else None
        except Exception:  # noqa: BLE001
            return None

    sem = asyncio.Semaphore(max(1, concurrency))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, deadline_s)

    async def _one(name: str) -> None:
        if loop.time() >= deadline:
            return
        async with sem:
            if loop.time() >= deadline:
                return
            session = _session_for(name)
            try:
                await manager.list_methods(name, session=session)
            except TypeError:
                # Fallback for stubs / older managers that don't accept
                # the session kwarg.
                try:
                    await manager.list_methods(name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("mcp warmup: %s failed: %s", name, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("mcp warmup: %s failed: %s", name, exc)

            list_resources = getattr(manager, "list_resources", None)
            if list_resources is not None:
                if loop.time() >= deadline:
                    return
                try:
                    await list_resources(name, session=session)
                except TypeError:
                    try:
                        await list_resources(name)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "mcp warmup resources: %s failed: %s", name, exc
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mcp warmup resources: %s failed: %s", name, exc
                    )

            list_prompts = getattr(manager, "list_prompts", None)
            if list_prompts is None:
                return
            if loop.time() >= deadline:
                return
            try:
                await list_prompts(name, session=session)
            except TypeError:
                try:
                    await list_prompts(name)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mcp warmup prompts: %s failed: %s", name, exc
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "mcp warmup prompts: %s failed: %s", name, exc
                )

    tasks = [asyncio.create_task(_one(s)) for s in servers]
    remaining = max(0.0, deadline - loop.time())
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
