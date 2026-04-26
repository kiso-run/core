"""Tests for MCP worker pre-flight argument validation.

Business requirement: before the worker invokes an MCP server, it
validates the task's args against the server's cached
``input_schema``. Bad args never reach the subprocess — they fail
the task with a human-readable reason that identifies the
offending field(s), so the replan picks up a concrete repair
signal rather than an opaque transport error.

Contract:
- Schema-valid args are dispatched as before; the handler calls
  ``MCPManager.call_method`` exactly once.
- Schema-invalid args short-circuit: ``call_method`` is NEVER
  called, the task transitions to ``failed``, and the replan
  reason contains the schema error text.
- If the cached schema is absent or empty (server hasn't been
  queried yet, or genuinely takes no args), the gate is
  permissive and the call goes through.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiso.config import Config, Provider
from kiso.mcp.schemas import MCPCallResult, MCPMethod
from kiso.store import create_plan, create_session, create_task, get_tasks_for_plan, init_db
from kiso.worker.loop import _TASK_HANDLERS, TASK_TYPE_MCP, _PlanCtx
from tests.conftest import full_models, full_settings


def _config():
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://example.com/v1")},
        users={},
        models=full_models(),
        settings=full_settings(briefer_enabled=False, bot_name="Kiso"),
        raw={},
    )


def _method(
    server: str, name: str, schema: dict | None = None
) -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description="",
        input_schema=schema if schema is not None else {},
        output_schema=None,
        annotations=None,
    )


class FakeManager:
    """Manager stub with both `call_method` and `list_methods_cached_only`."""

    def __init__(
        self,
        *,
        methods: list[MCPMethod] | None = None,
        return_value=None,
        available: bool = True,
    ) -> None:
        self._methods = methods or []
        self._return_value = return_value or MCPCallResult(
            stdout_text="ok",
            published_files=[],
            structured_content=None,
            is_error=False,
        )
        self._available = available
        self.call_args: tuple | None = None

    def is_available(self, name: str) -> bool:
        return self._available

    def list_methods_cached_only(self, name: str) -> list[MCPMethod]:
        return [m for m in self._methods if m.server == name]

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
        return self._return_value


@pytest.fixture()
async def db(tmp_path, monkeypatch):
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


async def _make_task(db, *, args: Any, server: str = "github", method: str = "create_issue"):
    pid = await create_plan(db, "s1", 0, "Test plan")
    await create_task(
        db, pid, "s1", "mcp", "call github create_issue",
        args=args,
        expect="ok",
        server=server,
        method=method,
    )
    rows = await get_tasks_for_plan(db, pid)
    return rows[0]


SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "labels": {"type": "array"},
    },
    "required": ["title", "body"],
}


class TestPreflight:
    async def test_valid_args_pass_through(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager(methods=[_method("github", "create_issue", SCHEMA)])
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_task(db, args={"title": "bug", "body": "x"})
        await handler(ctx, task_row, 0, True, 0)
        assert mgr.call_args is not None
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "done"

    async def test_missing_required_short_circuits(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager(methods=[_method("github", "create_issue", SCHEMA)])
        ctx = await _make_ctx(db, mgr)
        # 'body' missing → schema violation
        task_row = await _make_task(db, args={"title": "bug"})
        await handler(ctx, task_row, 0, True, 0)
        # Subprocess is never called
        assert mgr.call_args is None
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"
        # Error text identifies the offending field
        combined = (rows[0].get("output") or "") + (rows[0].get("stderr") or "")
        assert "body" in combined

    async def test_wrong_type_short_circuits(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager(methods=[_method("github", "create_issue", SCHEMA)])
        ctx = await _make_ctx(db, mgr)
        # 'labels' must be array — passing a string
        task_row = await _make_task(
            db,
            args={"title": "bug", "body": "x", "labels": "not-an-array"},
        )
        await handler(ctx, task_row, 0, True, 0)
        assert mgr.call_args is None
        rows = await get_tasks_for_plan(db, task_row["plan_id"])
        assert rows[0]["status"] == "failed"
        combined = (rows[0].get("output") or "") + (rows[0].get("stderr") or "")
        assert "labels" in combined

    async def test_empty_schema_is_permissive(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        # Method with empty schema — anything goes
        mgr = FakeManager(
            methods=[_method("github", "create_issue", {})]
        )
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_task(db, args={"anything": "goes"})
        await handler(ctx, task_row, 0, True, 0)
        assert mgr.call_args is not None

    async def test_schema_absent_is_permissive(self, db):
        """Server never queried → no cached methods → no schema."""
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = FakeManager(methods=[])  # no cached method
        ctx = await _make_ctx(db, mgr)
        task_row = await _make_task(db, args={"whatever": "x"})
        await handler(ctx, task_row, 0, True, 0)
        # With no cached schema we let the call through;
        # the subprocess will decide whether the args are valid.
        assert mgr.call_args is not None
