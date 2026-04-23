"""Background pre-loader for the MCP method catalog.

``MCPManager.list_methods_cached_only`` is empty until someone calls
``list_methods`` — which means the first user message after a daemon
boot sees a cold catalog and the planner may route via exec instead
of the MCP method that would otherwise be available.

``warm_catalog(manager)`` pre-loads the catalog in the background,
bounded by concurrency and a total wall-clock deadline. Per-server
failures are isolated (logged + skipped). Callers fire it with
``asyncio.create_task`` during daemon boot — they do NOT await it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


log = logging.getLogger(__name__)


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

    sem = asyncio.Semaphore(max(1, concurrency))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, deadline_s)

    async def _one(name: str) -> None:
        if loop.time() >= deadline:
            return
        async with sem:
            if loop.time() >= deadline:
                return
            try:
                await manager.list_methods(name, session=None)
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
            if list_resources is None:
                return
            if loop.time() >= deadline:
                return
            try:
                await list_resources(name, session=None)
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
