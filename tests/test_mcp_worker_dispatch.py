"""Tests for the worker-side MCP task handler ``_handle_mcp_task``.

Uses a fake ``MCPManager`` stand-in and builds a minimal plan
context to exercise the dispatch path end-to-end without a real
subprocess or HTTP call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiso.mcp.schemas import (
    MCPCallResult,
    MCPInvocationError,
    MCPTransportError,
)
from kiso.worker.loop import _TASK_HANDLERS, _PlanCtx, TASK_TYPE_MCP
from kiso.store import (
    create_plan,
    create_session,
    create_task,
    get_tasks_for_plan,
    init_db,
)
from tests.conftest import full_models, full_settings


class FakeManager:
    """Minimal MCPManager stand-in for _handle_mcp_task tests."""

    def __init__(self, *, return_value=None, exc=None, available: bool = True) -> None:
        self._return_value = return_value
        self._exc = exc
        self._available = available
        self.call_args: tuple | None = None

    def is_available(self, name: str) -> bool:
        return self._available

    def list_methods_cached_only(self, name: str) -> list:
        return []

    async def call_method(
        self,
        server: str,
        method: str,
        args: dict,
        *,
        session: str | None = None,
        sandbox_uid: int | None = None,
    ):
        self.call_args = (server, method, args, session, sandbox_uid)
        if self._exc is not None:
            raise self._exc
        return self._return_value


def _config():
    from kiso.config import Config, Provider

    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://example.com/v1")},
        users={},
        models=full_models(),
        settings=full_settings(briefer_enabled=False, bot_name="Kiso"),
        raw={},
    )


@pytest.fixture()
async def db(tmp_path, monkeypatch):
    # Point KISO_DIR at a temp dir so _session_workspace(session)
    # resolves under the test tmp_path.
    import kiso.config as cfgmod
    import kiso.worker.utils as utilsmod

    monkeypatch.setattr(cfgmod, "KISO_DIR", tmp_path)
    monkeypatch.setattr(utilsmod, "KISO_DIR", tmp_path)

    conn = await init_db(tmp_path / "test.db")
    await create_session(conn, "s1")
    yield conn
    await conn.close()


async def _make_ctx(db, mcp_manager) -> _PlanCtx:
    return _PlanCtx(
        db=db,
        config=_config(),
        session="s1",
        goal="test",
        user_message="hi",
        deploy_secrets={},
        session_secrets={},
        max_output_size=65536,
        max_worker_retries=3,
        messenger_timeout=30,
        slog=None,
        sandbox_uid=None,
        mcp_manager=mcp_manager,
    )


async def _make_mcp_task_row(db, task_type: str = "mcp", **task_overrides):
    pid = await create_plan(db, "s1", 0, "Test plan")
    task_data = {
        "type": task_type,
        "detail": "call github create_issue",
        "wrapper": None,
        "args": {"title": "bug", "body": "x"},
        "expect": "issue",
        "server": "github",
        "method": "create_issue",
    }
    task_data.update(task_overrides)
    await create_task(
        db, pid, "s1", task_type,
        task_data["detail"],
        wrapper=task_data["wrapper"],
        args=task_data["args"],
        expect=task_data["expect"],
        server=task_data.get("server"),
        method=task_data.get("method"),
    )
    rows = await get_tasks_for_plan(db, pid)
    return rows[0]


class TestHappyPath:
    async def test_manager_called_with_correct_args(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        result_payload = MCPCallResult(
            stdout_text="issue created",
            published_files=[],
            structured_content=None,
            is_error=False,
        )
        mgr = FakeManager(return_value=result_payload)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(db)
        handler_result = await handler(ctx, task_row, 0, True, 0)
        assert mgr.call_args[:3] == ("github", "create_issue", {"title": "bug", "body": "x"})
        assert mgr.call_args[3] is not None
        assert handler_result.stop is False or handler_result.stop_success is not False
        # Task marked done in DB
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert any(r["status"] == "done" for r in rows)

    async def test_sandbox_uid_from_ctx_forwarded_to_manager(self, db):
        """A user-role session has ctx.sandbox_uid set; the handler must
        relay it so MCPManager spawns the stdio subprocess under the
        session's UID (parity with exec)."""
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        payload = MCPCallResult(
            stdout_text="ok",
            published_files=[],
            structured_content=None,
            is_error=False,
        )
        mgr = FakeManager(return_value=payload)
        ctx = await _make_ctx(db, mgr)
        ctx.sandbox_uid = 4242
        task_row = await _make_mcp_task_row(db)
        await handler(ctx, task_row, 0, True, 0)
        # FakeManager stores (server, method, args, session, sandbox_uid)
        assert mgr.call_args[4] == 4242

    async def test_stdout_text_stored_in_task_output(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        payload = MCPCallResult(
            stdout_text="issue #42 opened",
            published_files=[],
            structured_content=None,
            is_error=False,
        )
        mgr = FakeManager(return_value=payload)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(db)
        await handler(ctx, task_row, 0, True, 0)
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["output"] == "issue #42 opened"


class TestErrorPaths:
    async def test_missing_server_fails_cleanly(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager()
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(db, server=None)
        result = await handler(ctx, task_row, 0, True, 0)
        assert result.stop is True or result.stop_replan is not None
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"

    async def test_no_manager_fails_cleanly(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        ctx = await _make_ctx(db, None)
        task_row = await _make_mcp_task_row(db)
        result = await handler(ctx, task_row, 0, True, 0)
        assert result.stop is True or result.stop_replan is not None
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"

    async def test_unavailable_server_fails(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager(available=False)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(db)
        result = await handler(ctx, task_row, 0, True, 0)
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"

    async def test_invocation_error_propagated_as_failure(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager(exc=MCPInvocationError("unknown method nope"))
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(db)
        result = await handler(ctx, task_row, 0, True, 0)
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"

    async def test_transport_error_propagated_as_failure(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager(exc=MCPTransportError("pipe closed"))
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(db)
        result = await handler(ctx, task_row, 0, True, 0)
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"

    async def test_is_error_result_marks_task_failed(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        payload = MCPCallResult(
            stdout_text="simulated server error",
            published_files=[],
            structured_content=None,
            is_error=True,
        )
        mgr = FakeManager(return_value=payload)
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_mcp_task_row(db)
        await handler(ctx, task_row, 0, True, 0)
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"
