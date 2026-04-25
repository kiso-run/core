"""M1560 / aider-mcp M11 Part B: Kiso runtime auto-fills
``architect_model`` and ``editor_model`` when the planner emits an
MCP call to ``kiso-aider:aider_codegen`` without them.

Caller-supplied values always win (setdefault semantics). Other MCP
servers are NEVER touched — the injection is namespaced to
kiso-aider only.
"""
from __future__ import annotations

import pytest

from kiso.mcp.schemas import MCPCallResult
from kiso.worker.loop import _TASK_HANDLERS, TASK_TYPE_MCP

from tests.test_mcp_worker_dispatch import (
    FakeManager,
    _config,
    _make_ctx,
    _make_mcp_task_row,
    db,  # fixture
)


_OK_RESULT = MCPCallResult(
    stdout_text="diff",
    published_files=[],
    structured_content=None,
    is_error=False,
)


class TestAiderModelInjection:
    """Pre-fill semantics for kiso-aider:aider_codegen calls."""

    async def test_both_models_missing_get_filled(self, db):
        """Planner emits args without architect_model / editor_model →
        runtime fills them from config.models.planner / .worker."""
        mgr = FakeManager(return_value=_OK_RESULT)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(
            db,
            server="kiso-aider", method="aider_codegen",
            args={"prompt": "fix the bug"},
            detail="ask aider to fix the bug",
        )
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        await handler(ctx, task_row, 0, True, 0)

        sent_args = mgr.call_args[2]
        assert sent_args["architect_model"] == ctx.config.models["planner"]
        assert sent_args["editor_model"] == ctx.config.models["worker"]

    async def test_architect_present_only_editor_filled(self, db):
        """Caller-supplied architect_model wins; editor still filled."""
        mgr = FakeManager(return_value=_OK_RESULT)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(
            db,
            server="kiso-aider", method="aider_codegen",
            args={
                "prompt": "fix",
                "architect_model": "openrouter/custom/architect",
            },
            detail="ask aider",
        )
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        await handler(ctx, task_row, 0, True, 0)

        sent_args = mgr.call_args[2]
        assert sent_args["architect_model"] == "openrouter/custom/architect"
        assert sent_args["editor_model"] == ctx.config.models["worker"]

    async def test_editor_present_only_architect_filled(self, db):
        mgr = FakeManager(return_value=_OK_RESULT)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(
            db,
            server="kiso-aider", method="aider_codegen",
            args={
                "prompt": "fix",
                "editor_model": "openrouter/custom/editor",
            },
            detail="ask aider",
        )
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        await handler(ctx, task_row, 0, True, 0)

        sent_args = mgr.call_args[2]
        assert sent_args["architect_model"] == ctx.config.models["planner"]
        assert sent_args["editor_model"] == "openrouter/custom/editor"

    async def test_both_present_no_change(self, db):
        """User explicit override: don't touch either field."""
        mgr = FakeManager(return_value=_OK_RESULT)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(
            db,
            server="kiso-aider", method="aider_codegen",
            args={
                "prompt": "fix",
                "architect_model": "x/A",
                "editor_model": "x/E",
            },
            detail="ask aider",
        )
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        await handler(ctx, task_row, 0, True, 0)

        sent_args = mgr.call_args[2]
        assert sent_args["architect_model"] == "x/A"
        assert sent_args["editor_model"] == "x/E"

    async def test_different_server_no_injection(self, db):
        """The injection is namespaced. A different MCP call must NOT
        receive architect_model / editor_model fillers."""
        mgr = FakeManager(return_value=_OK_RESULT)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(
            db,
            server="kiso-search", method="web_search",
            args={"query": "python release"},
            detail="search the web",
        )
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        await handler(ctx, task_row, 0, True, 0)

        sent_args = mgr.call_args[2]
        assert "architect_model" not in sent_args
        assert "editor_model" not in sent_args
        assert sent_args == {"query": "python release"}

    async def test_aider_doctor_no_injection(self, db):
        """Same server, different method (doctor) — no injection.
        Only `aider_codegen` accepts architect/editor model knobs."""
        mgr = FakeManager(return_value=_OK_RESULT)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(
            db,
            server="kiso-aider", method="doctor",
            args={},
            detail="check aider health",
        )
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        await handler(ctx, task_row, 0, True, 0)

        sent_args = mgr.call_args[2]
        assert "architect_model" not in sent_args
        assert "editor_model" not in sent_args
