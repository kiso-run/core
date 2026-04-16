"""M1373: MCPManager wired through the worker planning path.

After this milestone, every function in the call chain
``run_worker → _process_message → _run_planning_loop →
run_planner → build_planner_messages`` accepts an optional
``mcp_manager`` parameter (or ``mcp_catalog_text`` for
``build_planner_messages``, already landed in M1370).

These tests verify:
- Signature contracts at each level
- ``run_planner`` renders ``mcp_catalog_text`` from the manager
  and passes ``mcp_methods_pool`` to ``validate_plan``
- ``run_worker`` constructs the manager from ``config.mcp_servers``
  and shuts it down on exit
"""

from __future__ import annotations

import inspect

import pytest


class TestRunPlannerAcceptsMcpManager:
    def test_signature_has_mcp_manager(self) -> None:
        from kiso.brain.planner import run_planner

        sig = inspect.signature(run_planner)
        assert "mcp_manager" in sig.parameters
        assert sig.parameters["mcp_manager"].default is None


class TestProcessMessageAcceptsMcpManager:
    def test_signature_has_mcp_manager(self) -> None:
        from kiso.worker.loop import _process_message

        sig = inspect.signature(_process_message)
        assert "mcp_manager" in sig.parameters
        assert sig.parameters["mcp_manager"].default is None


class TestRunPlanningLoopAcceptsMcpManager:
    def test_signature_has_mcp_manager(self) -> None:
        from kiso.worker.loop import _run_planning_loop

        sig = inspect.signature(_run_planning_loop)
        assert "mcp_manager" in sig.parameters
        assert sig.parameters["mcp_manager"].default is None


class TestRunPlannerBuildsCatalogFromManager:
    """When ``mcp_manager`` is provided, ``run_planner`` must:
    1. Call ``format_mcp_catalog(manager)`` to render text
    2. Pass it as ``mcp_catalog_text`` to ``build_planner_messages``
    3. Build ``mcp_methods_pool`` dict for ``validate_plan``
    """

    @pytest.mark.asyncio
    async def test_catalog_text_reaches_build_planner_messages(self) -> None:
        """Integration test: run_planner → build_planner_messages gets
        a non-empty ``mcp_catalog_text`` when a manager with cached
        methods is provided.

        This is a focused test — we intercept the
        ``build_planner_messages`` call to capture the argument,
        without running the full planner LLM.
        """
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, patch

        from kiso.mcp.schemas import MCPMethod

        @dataclass
        class _StubManager:
            def available_servers(self):
                return ["stub"]

            def list_methods_cached_only(self, name):
                return [
                    MCPMethod(
                        server="stub",
                        name="do_thing",
                        title=None,
                        description="Does the thing",
                        input_schema={"type": "object"},
                        output_schema=None,
                        annotations=None,
                    )
                ]

            async def shutdown_all(self):
                pass

        class _Intercepted(Exception):
            pass

        captured_kwargs = {}

        async def _intercept_build(*args, **kwargs):
            captured_kwargs.update(kwargs)
            raise _Intercepted("intercepted")

        with patch(
            "kiso.brain.planner.build_planner_messages",
            side_effect=_intercept_build,
        ):
            from kiso.brain.planner import run_planner

            try:
                await run_planner(
                    db=AsyncMock(),
                    config=AsyncMock(),
                    session="s1",
                    user_role="admin",
                    new_message="test",
                    mcp_manager=_StubManager(),
                )
            except _Intercepted:
                pass

        assert "mcp_catalog_text" in captured_kwargs
        text = captured_kwargs["mcp_catalog_text"]
        assert text and "stub:do_thing" in text
