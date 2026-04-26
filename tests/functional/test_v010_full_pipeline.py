"""End-to-end v0.10 pipeline test.

Exercises the full pipeline (classifier → briefer → planner → worker
MCP dispatch → messenger) with a fixture skill loaded from disk and a
fixture echo MCP server spawned as a subprocess. LLM calls are mocked
with role-keyed pre-canned responses so the assertions can target
plumbing rather than LLM behaviour:

- The fixture skill's ``## Planner`` body lands in the planner prompt.
- The planner emits an ``mcp`` task with the expected server/method.
- The worker dispatches the task via the ``MCPManager`` under the
  session scope, and the persisted row keeps the ``expect`` contract
  the reviewer would consume on a failure path.
- The messenger output includes the echoed payload.

A second class covers session-scoped MCP isolation: two concurrent
sessions running against a server whose config references
``${session:workspace}`` get independent subprocesses and independent
workspaces.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from kiso.mcp.config import MCPServer
from kiso.mcp.manager import MCPManager
from kiso.skill_loader import invalidate_skills_cache
from kiso.store import create_session, save_message
from kiso.worker.loop import _process_message

pytestmark = pytest.mark.functional


FIXTURES = Path(__file__).parent / "fixtures"
ECHO_SERVER_PATH = FIXTURES / "mcp" / "echo-server" / "server.py"
PYTHON_DEBUG_SKILL = FIXTURES / "skills" / "python-debug"


# ---------------------------------------------------------------------------
# Helpers — mocked LLM responses, role-keyed
# ---------------------------------------------------------------------------


def _briefing_json(skill: str | None, mcp_method: str | None) -> str:
    return json.dumps({
        "modules": ["planning_rules", "skills_and_mcp"],
        "skills": [skill] if skill else [],
        "mcp_methods": [mcp_method] if mcp_method else [],
        "mcp_resources": [],
        "mcp_prompts": [],
        "context": "",
        "output_indices": [],
        "relevant_tags": [],
        "relevant_entities": [],
    })


def _plan_json(
    message: str,
    *,
    server: str = "echo",
    method: str = "echo",
    persist: bool = False,
) -> str:
    args: dict = {"message": message}
    if persist:
        args["persist"] = True
    return json.dumps({
        "goal": "Echo the user snippet and report back",
        "secrets": None,
        "tasks": [
            {
                "type": "mcp",
                "detail": f"Echo the snippet via {server}:{method}",
                "args": args,
                "expect": "the echoed message",
                "server": server,
                "method": method,
            },
            {
                "type": "msg",
                "detail": (
                    "Answer in English. Tell the user what the echo "
                    "returned."
                ),
                "args": None,
                "expect": None,
            },
        ],
        "extend_replan": None,
        "needs_install": None,
        "knowledge": None,
        "kb_answer": None,
    })


def _review_ok_json(summary: str = "echo task completed") -> str:
    return json.dumps({
        "status": "ok",
        "reason": None,
        "learn": None,
        "retry_hint": None,
        "summary": summary,
    })


def _curator_json() -> str:
    return json.dumps({
        "entity_assignments": [],
        "tag_suggestions": [],
        "facts": [],
    })


def _install_skill_snapshot(kiso_dir: Path) -> None:
    """Copy the fixture skill into the test KISO_DIR and clear cache."""
    dst = kiso_dir / "skills" / "python-debug"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(PYTHON_DEBUG_SKILL, dst)
    invalidate_skills_cache()


def _echo_server(name: str = "echo", *, session_scoped: bool = False) -> MCPServer:
    """Build an MCPServer config for the fixture echo server.

    When *session_scoped* is True the server declares an env variable
    that references ``${session:workspace}``, forcing the manager to
    spawn one subprocess per session and to materialise a session
    workspace.
    """
    env: dict[str, str] = {}
    if session_scoped:
        env["ECHO_WORKSPACE"] = "${session:workspace}"
    return MCPServer(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=[str(ECHO_SERVER_PATH)],
        env=env,
        enabled=True,
        timeout_s=10.0,
        sandbox="never",
    )


# ---------------------------------------------------------------------------
# Role-keyed call_llm mock
# ---------------------------------------------------------------------------


class _RoleLLM:
    """Record every call_llm invocation and reply based on role.

    The test asserts against ``calls_by_role[role]`` — each entry is the
    full ``messages`` list passed to ``call_llm`` at that point, which
    lets us verify the planner prompt contained the fixture skill's
    ``## Planner`` body.
    """

    def __init__(self, plan_payload: str, echo_message: str) -> None:
        self.plan_payload = plan_payload
        self.echo_message = echo_message
        self.calls_by_role: dict[str, list[list[dict]]] = {}

    def respond(self, role: str, messages: list[dict]) -> str:
        self.calls_by_role.setdefault(role, []).append(
            [dict(m) for m in messages]
        )
        if role == "classifier":
            return "plan:English"
        if role == "briefer":
            return _briefing_json("python-debug", "echo:echo")
        if role == "planner":
            return self.plan_payload
        if role == "reviewer":
            return _review_ok_json()
        if role == "messenger":
            return (
                f"The echo server replied with {self.echo_message!r}."
            )
        if role == "curator":
            return _curator_json()
        if role == "summarizer":
            return "Session summary stub."
        if role == "paraphraser":
            return ""
        return ""


def _make_call_llm(llm: "_RoleLLM"):
    """Return a bound async function suitable as the ``call_llm`` seam."""

    async def _call(config, role, messages, **kwargs):
        return llm.respond(role, messages)

    return _call


def _joined(messages: list[dict]) -> str:
    return "\n".join(
        (m.get("content") or "") if isinstance(m, dict) else ""
        for m in messages
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def pipeline_db(tmp_path: Path):
    """Fresh DB per test."""
    from kiso.store import init_db
    conn = await init_db(tmp_path / "pipeline.db")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture()
def pipeline_session() -> str:
    return f"pipe-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# TestV010Pipeline — mocked LLM, one shot end-to-end
# ---------------------------------------------------------------------------


class TestV010Pipeline:
    async def test_mocked_end_to_end_routes_through_mcp(
        self,
        func_config,
        _func_kiso_dir,
        pipeline_db,
        pipeline_session,
    ):
        """Assert the full v0.10 pipeline routes a user message through
        the fixture skill and the fixture echo MCP server.
        """
        _install_skill_snapshot(_func_kiso_dir)
        echo_msg = "the python snippet to echo"
        llm = _RoleLLM(
            plan_payload=_plan_json(echo_msg),
            echo_message=echo_msg,
        )
        manager = MCPManager({"echo": _echo_server()})

        await create_session(pipeline_db, pipeline_session)
        msg_id = await save_message(
            pipeline_db, pipeline_session, "testadmin", "user",
            "please debug this python snippet",
        )
        msg = {
            "id": msg_id,
            "content": "please debug this python snippet",
            "user_role": "admin",
            "user_mcp": "*",
            "user_skills": "*",
            "username": "testadmin",
            "base_url": "http://test",
        }

        try:
            with patch(
                "kiso.brain.call_llm", new=_make_call_llm(llm),
            ):
                bg_task = await asyncio.wait_for(
                    _process_message(
                        pipeline_db,
                        func_config,
                        pipeline_session,
                        msg,
                        cancel_event=asyncio.Event(),
                        llm_timeout=func_config.settings["llm_timeout"],
                        max_replan_depth=func_config.settings["max_replan_depth"],
                        mcp_manager=manager,
                    ),
                    timeout=30,
                )
                if bg_task is not None and not bg_task.done():
                    try:
                        await asyncio.wait_for(bg_task, timeout=10)
                    except asyncio.TimeoutError:
                        bg_task.cancel()
        finally:
            await manager.shutdown_all()

        # --- Assertion 1: planner prompt contains the fixture skill's
        # ## Planner section body. ---
        planner_calls = llm.calls_by_role.get("planner", [])
        assert planner_calls, "planner was never called"
        planner_text = _joined(planner_calls[-1])
        assert "python-debug" in planner_text, (
            "planner prompt missing fixture skill name"
        )
        assert "echo:echo" in planner_text, (
            "planner prompt missing the fixture skill's Planner body"
        )

        # --- Assertion 2: the plan was persisted and ran to completion.
        cur = await pipeline_db.execute(
            "SELECT id, status FROM plans WHERE session = ? ORDER BY id",
            (pipeline_session,),
        )
        plans = [dict(r) for r in await cur.fetchall()]
        assert plans, "no plan was created"
        assert plans[-1]["status"] == "done", (
            f"final plan status is {plans[-1]['status']!r} — expected 'done'"
        )

        cur = await pipeline_db.execute(
            "SELECT id, type, status, output, server, method, expect "
            "FROM tasks WHERE plan_id = ? ORDER BY id",
            (plans[-1]["id"],),
        )
        tasks = [dict(r) for r in await cur.fetchall()]

        # --- Assertion 3: the worker dispatched the MCP task and got
        # the echo payload back. ---
        mcp_tasks = [t for t in tasks if t["type"] == "mcp"]
        assert len(mcp_tasks) == 1, (
            f"expected exactly one mcp task, got {len(mcp_tasks)}"
        )
        mcp_task = mcp_tasks[0]
        assert mcp_task["status"] == "done", (
            f"mcp task status is {mcp_task['status']!r}; output: "
            f"{mcp_task['output']!r}"
        )
        assert mcp_task["server"] == "echo"
        assert mcp_task["method"] == "echo"
        assert echo_msg in (mcp_task["output"] or ""), (
            f"echo task output does not include the echoed payload: "
            f"{mcp_task['output']!r}"
        )

        # --- Assertion 4: the reviewer contract is visible on the
        # persisted mcp task. Successful MCP dispatch short-circuits
        # the reviewer LLM call by design (only failures or exec tasks
        # route through the reviewer), so the check here is that the
        # ``expect`` field survives persistence — that is what the
        # reviewer sees on replay. ---
        assert mcp_task.get("expect") == "the echoed message", (
            f"mcp task expect field missing or altered: {mcp_task.get('expect')!r}"
        )

        # --- Assertion 5: the messenger final output includes the echo
        # content. ---
        msg_tasks = [t for t in tasks if t["type"] == "msg"]
        assert msg_tasks, "messenger task was never persisted"
        msg_output = msg_tasks[-1]["output"] or ""
        assert echo_msg in msg_output, (
            f"messenger output does not include the echo payload: "
            f"{msg_output!r}"
        )


# ---------------------------------------------------------------------------
# TestV010SessionScopedMCP — two concurrent sessions, per-session workspaces
# ---------------------------------------------------------------------------


class TestV010SessionScopedMCP:
    async def test_two_sessions_get_isolated_workspaces(
        self,
        func_config,
        _func_kiso_dir,
        tmp_path,
    ):
        """Two concurrent sessions using an MCP server with
        ``${session:workspace}`` get independent subprocess workspaces.

        The echo fixture writes ``echo.txt`` into ``$ECHO_WORKSPACE``
        (interpolated to each session's workspace) when called with
        ``persist=true``. After two parallel sessions, each session
        workspace must have its own ``echo.txt`` with its own payload.
        """
        _install_skill_snapshot(_func_kiso_dir)
        manager = MCPManager({"echo": _echo_server(session_scoped=True)})

        messages_by_session = {
            "sess-A": "alpha payload",
            "sess-B": "beta payload",
        }

        # One LLM router keyed by session. `unittest.mock.patch` is
        # process-global and does not stack safely across concurrent
        # asyncio tasks, so we patch ONCE around the gather, and the
        # mock routes responses by the ``session`` kwarg.
        session_llms: dict[str, _RoleLLM] = {
            sid: _RoleLLM(
                plan_payload=_plan_json(payload, persist=True),
                echo_message=payload,
            )
            for sid, payload in messages_by_session.items()
        }

        async def routed_call_llm(config, role, messages, **kwargs):
            sid = kwargs.get("session") or ""
            llm = session_llms.get(sid)
            if llm is None:
                raise AssertionError(
                    f"unexpected session in call_llm: {sid!r}"
                )
            return llm.respond(role, messages)

        async def run_one(session_id: str) -> None:
            from kiso.store import init_db
            db_path = tmp_path / f"{session_id}.db"
            db = await init_db(db_path)
            try:
                await create_session(db, session_id)
                msg_id = await save_message(
                    db, session_id, "testadmin", "user", "run echo please",
                )
                msg = {
                    "id": msg_id,
                    "content": "run echo please",
                    "user_role": "admin",
                    "user_mcp": "*",
                    "user_skills": "*",
                    "username": "testadmin",
                    "base_url": "http://test",
                }
                bg = await asyncio.wait_for(
                    _process_message(
                        db, func_config, session_id, msg,
                        cancel_event=asyncio.Event(),
                        llm_timeout=func_config.settings["llm_timeout"],
                        max_replan_depth=func_config.settings[
                            "max_replan_depth"
                        ],
                        mcp_manager=manager,
                    ),
                    timeout=30,
                )
                if bg is not None and not bg.done():
                    try:
                        await asyncio.wait_for(bg, timeout=5)
                    except asyncio.TimeoutError:
                        bg.cancel()
            finally:
                await db.close()

        try:
            with patch("kiso.brain.call_llm", new=routed_call_llm):
                await asyncio.gather(
                    *(run_one(s) for s in messages_by_session)
                )
        finally:
            await manager.shutdown_all()

        # --- Assertion: each session got its own echo.txt in its own
        # workspace with its own payload. ---
        for session_id, payload in messages_by_session.items():
            ws = _func_kiso_dir / "sessions" / session_id
            echo_file = ws / "echo.txt"
            assert echo_file.is_file(), (
                f"session {session_id} missing echo.txt at {echo_file}"
            )
            content = echo_file.read_text(encoding="utf-8")
            assert content == payload, (
                f"session {session_id} echo.txt has {content!r}; "
                f"expected {payload!r}"
            )

        # Cross-isolation: each session's workspace only has ITS payload.
        a_text = (_func_kiso_dir / "sessions" / "sess-A" / "echo.txt").read_text()
        b_text = (_func_kiso_dir / "sessions" / "sess-B" / "echo.txt").read_text()
        assert a_text != b_text
