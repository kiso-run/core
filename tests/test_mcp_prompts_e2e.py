"""End-to-end prompts flow test: stdio server → manager → worker.

Exercises the full prompts path without mocking the stdio transport
or the manager: a real subprocess MCP server
(``mcp_mock_stdio_server.py`` with scenario=``prompts_happy``) is
spawned, ``MCPManager.list_prompts`` primes the catalog, and the
worker ``_handle_mcp_task`` handler dispatches a
``{method: "__prompt_get", args: {name, prompt_args}}`` task through
the manager back to the subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.manager import MCPManager
from kiso.worker.loop import _TASK_HANDLERS, _PlanCtx, TASK_TYPE_MCP
from kiso.store import (
    create_plan,
    create_session,
    create_task,
    get_tasks_for_plan,
    init_db,
)
from tests.conftest import full_models, full_settings


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_mock_stdio_server.py"


def _server(scenario: str = "prompts_happy") -> MCPServer:
    return MCPServer(
        name="mock",
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE)],
        env={"MOCK_MCP_SCENARIO": scenario},
        cwd=None,
        enabled=True,
        timeout_s=10.0,
    )


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
        goal="fetch prompt",
        user_message="please get a code review prompt",
        deploy_secrets={},
        session_secrets={},
        max_output_size=65536,
        max_worker_retries=3,
        messenger_timeout=30,
        slog=None,
        sandbox_uid=None,
        mcp_manager=mcp_manager,
    )


class TestPromptsEndToEnd:
    async def test_list_and_get_via_real_subprocess(self, db):
        mgr = MCPManager({"mock": _server("prompts_happy")})
        try:
            prompts = await mgr.list_prompts("mock")
            names = {p.name for p in prompts}
            assert names == {"code_review", "translate"}

            cached = mgr.list_prompts_cached_only("mock")
            assert {p.name for p in cached} == names

            rendered = await mgr.get_prompt(
                "mock", "code_review", {"repo": "kiso-run", "focus": "mcp"},
            )
            assert rendered.description == "rendered:code_review"
            assert len(rendered.messages) == 1
            assert rendered.messages[0].role == "user"
            assert "Review kiso-run focusing on mcp." in rendered.messages[0].text
        finally:
            await mgr.shutdown_all()

    async def test_worker_dispatches_prompt_get_through_manager(self, db):
        handler = _TASK_HANDLERS[TASK_TYPE_MCP]
        mgr = MCPManager({"mock": _server("prompts_happy")})
        try:
            ctx = await _make_ctx(db, mgr)
            pid = await create_plan(db, "s1", 0, "prompt")
            await create_task(
                db, pid, "s1", "mcp",
                "fetch code review prompt",
                args={
                    "name": "code_review",
                    "prompt_args": {"repo": "kiso-run", "focus": "mcp"},
                },
                expect="prompt body",
                server="mock",
                method="__prompt_get",
            )
            rows = await get_tasks_for_plan(db, pid)
            task_row = rows[0]
            await handler(ctx, task_row, 0, True, 0)
            rows = await get_tasks_for_plan(db, pid)
            assert rows[0]["status"] == "done"
            out = rows[0]["output"]
            assert "Review kiso-run focusing on mcp." in out
            assert "[prompt: mock:code_review]" in out
            assert "user:" in out
        finally:
            await mgr.shutdown_all()
