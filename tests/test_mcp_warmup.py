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
        resources: dict[str, list] | None = None,
        resource_errors: dict[str, Exception] | None = None,
        prompts: dict[str, list] | None = None,
        prompt_errors: dict[str, Exception] | None = None,
    ) -> None:
        self._available = available
        self._methods = methods or {}
        self._delays = delays or {}
        self._errors = errors or {}
        self._resources = resources or {}
        self._resource_errors = resource_errors or {}
        self._prompts = prompts or {}
        self._prompt_errors = prompt_errors or {}
        self.calls: list[str] = []
        self.resource_calls: list[str] = []
        self.prompt_calls: list[str] = []

    def available_servers(self) -> list[str]:
        return list(self._available)

    async def list_methods(self, name: str, *, session=None):
        self.calls.append(name)
        if name in self._delays:
            await asyncio.sleep(self._delays[name])
        if name in self._errors:
            raise self._errors[name]
        return list(self._methods.get(name, []))

    async def list_resources(self, name: str, *, session=None):
        self.resource_calls.append(name)
        if name in self._resource_errors:
            raise self._resource_errors[name]
        return list(self._resources.get(name, []))

    async def list_prompts(self, name: str, *, session=None):
        self.prompt_calls.append(name)
        if name in self._prompt_errors:
            raise self._prompt_errors[name]
        return list(self._prompts.get(name, []))


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

    async def test_warms_resources_alongside_methods(self):
        mgr = FakeManager(
            available=["a", "b"],
            resources={"a": ["r1"], "b": []},
        )
        await warm_catalog(mgr)
        assert sorted(mgr.calls) == ["a", "b"]
        assert sorted(mgr.resource_calls) == ["a", "b"]

    async def test_resource_failure_does_not_affect_methods(self):
        mgr = FakeManager(
            available=["a"],
            resource_errors={"a": RuntimeError("no resources")},
        )
        await warm_catalog(mgr)
        assert mgr.calls == ["a"]
        assert mgr.resource_calls == ["a"]

    async def test_warms_prompts_alongside_methods(self):
        mgr = FakeManager(
            available=["a", "b"],
            prompts={"a": ["p1"], "b": []},
        )
        await warm_catalog(mgr)
        assert sorted(mgr.calls) == ["a", "b"]
        assert sorted(mgr.prompt_calls) == ["a", "b"]

    async def test_prompt_failure_does_not_affect_methods(self):
        mgr = FakeManager(
            available=["a"],
            prompt_errors={"a": RuntimeError("no prompts")},
        )
        await warm_catalog(mgr)
        assert mgr.calls == ["a"]
        assert mgr.prompt_calls == ["a"]

    async def test_manager_without_list_resources_still_warms_methods(self):
        """Older managers (pre-resources) that lack ``list_resources``
        must not break warmup."""

        class OldManager:
            def __init__(self):
                self.calls: list[str] = []

            def available_servers(self):
                return ["a"]

            async def list_methods(self, name, *, session=None):
                self.calls.append(name)
                return []

        mgr = OldManager()
        await warm_catalog(mgr)
        assert mgr.calls == ["a"]
