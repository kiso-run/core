"""Tests for ``kiso.mcp.warmup.warm_catalog``.

Business requirement: on daemon boot the first message's planner
would otherwise see an empty MCP catalog (the manager's
``list_methods_cached_only`` returns ``[]`` until someone explicitly
calls ``list_methods``). ``warm_catalog`` pre-loads the catalog for
every configured, enabled, healthy MCP server in the background,
bounded by concurrency and a total deadline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiso.mcp.warmup import warm_catalog


class FakeManager:
    def __init__(
        self,
        available: list[str],
        *,
        methods: dict[str, list] | None = None,
        delays: dict[str, float] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._available = available
        self._methods = methods or {}
        self._delays = delays or {}
        self._errors = errors or {}
        self.calls: list[str] = []

    def available_servers(self) -> list[str]:
        return list(self._available)

    async def list_methods(self, name: str, *, session=None):
        self.calls.append(name)
        if name in self._delays:
            await asyncio.sleep(self._delays[name])
        if name in self._errors:
            raise self._errors[name]
        return list(self._methods.get(name, []))


class TestWarmCatalog:
    async def test_calls_list_methods_for_every_server(self):
        mgr = FakeManager(available=["a", "b", "c"])
        await warm_catalog(mgr)
        assert sorted(mgr.calls) == ["a", "b", "c"]

    async def test_respects_concurrency_bound(self):
        """With concurrency=1 and 3 slow servers, calls happen sequentially."""
        mgr = FakeManager(
            available=["a", "b", "c"],
            delays={"a": 0.05, "b": 0.05, "c": 0.05},
        )
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await warm_catalog(mgr, concurrency=1, deadline_s=10.0)
        elapsed = loop.time() - t0
        assert elapsed >= 0.14  # 3 × 50ms (loose margin)

    async def test_concurrency_above_one_runs_in_parallel(self):
        mgr = FakeManager(
            available=["a", "b", "c"],
            delays={"a": 0.05, "b": 0.05, "c": 0.05},
        )
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await warm_catalog(mgr, concurrency=3, deadline_s=10.0)
        elapsed = loop.time() - t0
        assert elapsed < 0.15  # should be close to 50ms, not 150ms

    async def test_server_failure_is_isolated(self):
        mgr = FakeManager(
            available=["a", "b"],
            errors={"a": RuntimeError("boom")},
        )
        await warm_catalog(mgr)
        assert "a" in mgr.calls
        assert "b" in mgr.calls

    async def test_deadline_aborts_remaining_servers(self):
        """Past the deadline, pending servers stop being warmed."""
        mgr = FakeManager(
            available=["slow", "also-slow"],
            delays={"slow": 0.5, "also-slow": 0.5},
        )
        await warm_catalog(mgr, concurrency=1, deadline_s=0.05)
        # Deadline hit after first server started; second should not run
        # (exact count depends on scheduler, but at least one remains
        # unattempted at concurrency=1).
        assert len(mgr.calls) < 2

    async def test_none_manager_is_noop(self):
        await warm_catalog(None)  # must not raise

    async def test_empty_manager_is_noop(self):
        mgr = FakeManager(available=[])
        await warm_catalog(mgr)
        assert mgr.calls == []
