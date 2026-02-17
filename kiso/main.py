"""FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

from kiso.auth import AuthInfo, require_auth, resolve_user
from kiso.config import KISO_DIR, load_config
from kiso.store import (
    create_session,
    get_all_sessions,
    get_plan_for_session,
    get_sessions_for_user,
    get_tasks_for_session,
    init_db,
    save_message,
)
from kiso.worker import run_worker

log = logging.getLogger(__name__)

SESSION_RE = re.compile(r"^[a-zA-Z0-9_@.\-]{1,255}$")

# Per-session workers: session → (queue, asyncio.Task)
_workers: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}


class MsgRequest(BaseModel):
    session: str
    user: str
    content: str


def _ensure_worker(session: str, db, config) -> asyncio.Queue:
    """Ensure a worker exists for the session. Returns the queue.

    Atomic: no await between checking and creating (prevents duplicate workers).
    """
    entry = _workers.get(session)
    if entry and not entry[1].done():
        return entry[0]
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(run_worker(db, config, session, queue))

    def _cleanup(t, s=session):
        _workers.pop(s, None)

    task.add_done_callback(_cleanup)
    _workers[session] = (queue, task)
    return queue


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = load_config()
    app.state.db = await init_db(KISO_DIR / "store.db")
    yield
    # Cancel all workers on shutdown
    for session, (queue, task) in _workers.items():
        task.cancel()
    for session, (queue, task) in list(_workers.items()):
        try:
            await task
        except asyncio.CancelledError:
            pass
    _workers.clear()
    await app.state.db.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/msg", status_code=202)
async def post_msg(
    body: MsgRequest,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if not SESSION_RE.match(body.session):
        raise HTTPException(status_code=400, detail="Invalid session ID")

    db = request.app.state.db
    config = request.app.state.config
    resolved = resolve_user(config, body.user, auth.token_name)

    await create_session(db, body.session, connector=auth.token_name)

    if resolved.trusted:
        msg_id = await save_message(
            db, body.session, resolved.username, "user", body.content,
            trusted=True, processed=False,
        )
        user_role = resolved.user.role if resolved.user else "user"
        user_skills = resolved.user.skills if resolved.user else None
        queue = _ensure_worker(body.session, db, config)
        await queue.put({
            "id": msg_id,
            "content": body.content,
            "user_role": user_role,
            "user_skills": user_skills,
        })
        return {"queued": True, "session": body.session, "message_id": msg_id}
    else:
        msg_id = await save_message(
            db, body.session, resolved.username, "user", body.content,
            trusted=False, processed=True,
        )
        return {"queued": False, "session": body.session, "untrusted": True, "message_id": msg_id}


@app.get("/status/{session}")
async def get_status(
    session: str,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    after: int = Query(0),
):
    db = request.app.state.db
    tasks = await get_tasks_for_session(db, session, after=after)
    plan = await get_plan_for_session(db, session)

    entry = _workers.get(session)
    worker_running = entry is not None and not entry[1].done()
    queue_length = entry[0].qsize() if entry else 0

    return {
        "tasks": tasks,
        "plan": plan,
        "queue_length": queue_length,
        "worker_running": worker_running,
        "active_task": None,
    }


@app.get("/sessions")
async def get_sessions(
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    user: str = Query(...),
    all: bool = Query(False),
):
    db = request.app.state.db
    config = request.app.state.config
    resolved = resolve_user(config, user, auth.token_name)

    if all and resolved.trusted and resolved.user and resolved.user.role == "admin":
        sessions = await get_all_sessions(db)
    else:
        sessions = await get_sessions_for_user(db, resolved.username)
    return sessions
