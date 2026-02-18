"""FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel
from starlette.responses import JSONResponse

from kiso.auth import AuthInfo, require_auth, resolve_user
from kiso.config import KISO_DIR, load_config
from kiso.store import (
    create_session,
    get_all_sessions,
    get_plan_for_session,
    get_sessions_for_user,
    get_tasks_for_session,
    get_unprocessed_trusted_messages,
    init_db,
    recover_stale_running,
    save_message,
    upsert_session,
)
from kiso.webhook import validate_webhook_url
from kiso.worker import run_worker

log = logging.getLogger(__name__)

SESSION_RE = re.compile(r"^[a-zA-Z0-9_@.\-]{1,255}$")

# Per-session workers: session → (queue, asyncio.Task, cancel_event)
_workers: dict[str, tuple[asyncio.Queue, asyncio.Task, asyncio.Event]] = {}


class SessionRequest(BaseModel):
    session: str
    webhook: str | None = None
    description: str | None = None


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
    maxsize = int(config.settings.get("max_queue_size", 50))
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        run_worker(db, config, session, queue, cancel_event=cancel_event)
    )

    def _cleanup(t, s=session):
        _workers.pop(s, None)

    task.add_done_callback(_cleanup)
    _workers[session] = (queue, task, cancel_event)
    return queue


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Skip comments and blank lines."""
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        result[key] = value
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config
    db = await init_db(KISO_DIR / "store.db")
    app.state.db = db

    # Startup recovery: mark stale running plans/tasks as failed
    plans_recovered, tasks_recovered = await recover_stale_running(db)
    if plans_recovered or tasks_recovered:
        log.info(
            "Startup recovery: %d stale plans, %d stale tasks marked failed",
            plans_recovered, tasks_recovered,
        )

    # Startup recovery: re-enqueue unprocessed trusted messages
    unprocessed = await get_unprocessed_trusted_messages(db)
    if unprocessed:
        from collections import defaultdict
        by_session: dict[str, list[dict]] = defaultdict(list)
        for msg in unprocessed:
            by_session[msg["session"]].append(msg)

        recovered_count = 0
        for sess_id, msgs in by_session.items():
            queue = _ensure_worker(sess_id, db, config)
            for msg in msgs:
                # Re-resolve user role/skills from current config
                resolved = resolve_user(config, msg["user"] or "", "")
                user_role = resolved.user.role if resolved.user else "user"
                user_skills = resolved.user.skills if resolved.user else None
                try:
                    queue.put_nowait({
                        "id": msg["id"],
                        "content": msg["content"],
                        "user_role": user_role,
                        "user_skills": user_skills,
                        "username": msg["user"],
                    })
                    recovered_count += 1
                except asyncio.QueueFull:
                    log.warning(
                        "Queue full for session=%s, skipping message %d",
                        sess_id, msg["id"],
                    )
        if recovered_count:
            log.info("Startup recovery: re-enqueued %d unprocessed messages", recovered_count)

    # Webhook secret length warning
    webhook_secret = config.settings.get("webhook_secret", "")
    if webhook_secret and len(webhook_secret) < 32:
        log.warning(
            "webhook_secret is only %d characters — recommend at least 32",
            len(webhook_secret),
        )

    yield

    # Graceful shutdown with timeout
    shutdown_timeout = int(config.settings.get("exec_timeout", 120))
    for session, (queue, task, cancel) in _workers.items():
        cancel.set()
    for session, (queue, task, _cancel) in list(_workers.items()):
        try:
            await asyncio.wait_for(task, timeout=shutdown_timeout)
        except asyncio.TimeoutError:
            log.warning(
                "Worker session=%s did not finish in %ds, force cancelling",
                session, shutdown_timeout,
            )
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
    _workers.clear()
    await app.state.db.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/sessions")
async def post_sessions(
    body: SessionRequest,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if not SESSION_RE.match(body.session):
        raise HTTPException(status_code=400, detail="Invalid session ID")

    config = request.app.state.config

    if body.webhook:
        try:
            validate_webhook_url(
                body.webhook,
                config.settings.get("webhook_allow_list"),
                require_https=bool(config.settings.get("webhook_require_https", True)),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    db = request.app.state.db
    session_row, created = await upsert_session(
        db, body.session, connector=auth.token_name,
        webhook=body.webhook, description=body.description,
    )

    status_code = 201 if created else 200
    return JSONResponse(
        content={"session": body.session, "created": created},
        status_code=status_code,
    )


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

    max_msg = int(config.settings.get("max_message_size", 65536))
    if len(body.content) > max_msg:
        raise HTTPException(status_code=413, detail="Message content too large")

    await create_session(db, body.session, connector=auth.token_name)

    if resolved.trusted:
        msg_id = await save_message(
            db, body.session, resolved.username, "user", body.content,
            trusted=True, processed=False,
        )
        user_role = resolved.user.role if resolved.user else "user"
        user_skills = resolved.user.skills if resolved.user else None
        queue = _ensure_worker(body.session, db, config)
        try:
            queue.put_nowait({
                "id": msg_id,
                "content": body.content,
                "user_role": user_role,
                "user_skills": user_skills,
                "username": resolved.username,
            })
        except asyncio.QueueFull:
            raise HTTPException(status_code=429, detail="Too many queued messages")
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
    queue_length = entry[0].qsize() if entry and not entry[1].done() else 0

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


@app.post("/sessions/{session}/cancel")
async def post_cancel(
    session: str,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if not SESSION_RE.match(session):
        raise HTTPException(status_code=400, detail="Invalid session ID")

    db = request.app.state.db

    entry = _workers.get(session)
    if entry is None or entry[1].done():
        return {"cancelled": False}

    plan = await get_plan_for_session(db, session)
    if plan is None or plan["status"] != "running":
        return {"cancelled": False}

    cancel_event = entry[2]
    cancel_event.set()
    return {"cancelled": True, "plan_id": plan["id"]}


@app.post("/admin/reload-env")
async def post_reload_env(
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    user: str = Query(...),
):
    config = request.app.state.config
    resolved = resolve_user(config, user, auth.token_name)

    if not resolved.trusted or not resolved.user or resolved.user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    env_vars = _load_env_file(KISO_DIR / ".env")
    for key, value in env_vars.items():
        os.environ[key] = value

    return {"reloaded": True, "keys_loaded": len(env_vars)}
