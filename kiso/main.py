"""FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.responses import JSONResponse

from kiso.auth import AuthInfo, require_auth, resolve_user
from kiso.config import KISO_DIR, load_config
from kiso.log import setup_logging
from kiso.pub import pub_token, resolve_pub_token
from kiso.store import (
    count_messages,
    create_session,
    get_all_sessions,
    get_plan_for_session,
    get_session,
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


class WorkerEntry(NamedTuple):
    """Per-session worker state: queue, asyncio task, and cancel event."""
    queue: asyncio.Queue
    task: asyncio.Task
    cancel_event: asyncio.Event


def _init_kiso_dirs() -> None:
    """Ensure ~/.kiso/ subdirectories exist and sync reference docs."""
    (KISO_DIR / "sys" / "bin").mkdir(parents=True, exist_ok=True)
    (KISO_DIR / "sys" / "ssh").mkdir(parents=True, exist_ok=True)
    (KISO_DIR / "reference").mkdir(parents=True, exist_ok=True)

    # Sync bundled reference docs to ~/.kiso/reference/
    import importlib.resources
    ref_pkg = importlib.resources.files("kiso") / "reference"
    dest = KISO_DIR / "reference"
    for src_file in ref_pkg.iterdir():
        if src_file.name.endswith(".md"):
            target = dest / src_file.name
            content = src_file.read_text(encoding="utf-8")
            # Only write if changed (avoid unnecessary writes)
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8")

# Per-session workers: session → WorkerEntry
_workers: dict[str, WorkerEntry] = {}


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
    if entry and not entry.task.done():
        return entry.queue
    maxsize = int(config.settings.get("max_queue_size", 50))
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        run_worker(db, config, session, queue, cancel_event=cancel_event)
    )

    def _cleanup(t, s=session):
        _workers.pop(s, None)

    task.add_done_callback(_cleanup)
    _workers[session] = WorkerEntry(queue, task, cancel_event)
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
    setup_logging()
    config = load_config()
    app.state.config = config
    _init_kiso_dirs()
    log.info("Server starting — host=%s port=%s",
             config.settings.get("host", "0.0.0.0"),
             config.settings.get("port", 8333))
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
    for session, entry in _workers.items():
        entry.cancel_event.set()
    for session, entry in list(_workers.items()):
        try:
            await asyncio.wait_for(entry.task, timeout=shutdown_timeout)
        except asyncio.TimeoutError:
            log.warning(
                "Worker session=%s did not finish in %ds, force cancelling",
                session, shutdown_timeout,
            )
            entry.task.cancel()
            try:
                await entry.task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
    _workers.clear()
    await app.state.db.close()
    log.info("Server shut down")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/pub/{token}/{filename:path}")
async def get_pub(token: str, filename: str, request: Request):
    """Serve a file from a session's pub/ directory. No authentication required."""
    config = request.app.state.config
    session = resolve_pub_token(token, config)
    if session is None:
        raise HTTPException(status_code=404, detail="Not found")

    pub_dir = KISO_DIR / "sessions" / session / "pub"
    file_path = (pub_dir / filename).resolve()

    # Path traversal guard
    if not str(file_path).startswith(str(pub_dir.resolve())):
        raise HTTPException(status_code=404, detail="Not found")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path=file_path, filename=Path(filename).name, media_type=media_type)


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
    verbose: bool = Query(False),
):
    db = request.app.state.db
    tasks = await get_tasks_for_session(db, session, after=after)
    plan = await get_plan_for_session(db, session)

    if not verbose:
        _strip_llm_verbose(tasks, plan)

    entry = _workers.get(session)
    worker_running = entry is not None and not entry.task.done()
    queue_length = entry.queue.qsize() if entry and not entry.task.done() else 0

    return {
        "tasks": tasks,
        "plan": plan,
        "queue_length": queue_length,
        "worker_running": worker_running,
        "active_task": None,
    }


def _strip_llm_verbose(tasks: list[dict], plan: dict | None) -> None:
    """Remove messages/response from llm_calls to keep default response compact."""
    import json as _json
    for obj in ([plan] if plan else []) + tasks:
        raw = obj.get("llm_calls")
        if not raw:
            continue
        try:
            calls = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        for c in calls:
            c.pop("messages", None)
            c.pop("response", None)
        obj["llm_calls"] = _json.dumps(calls)


@app.get("/sessions/{session}/info")
async def get_session_info(
    session: str,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    """Return message count and summary snippet for a session."""
    db = request.app.state.db
    msg_count = await count_messages(db, session)
    sess = await get_session(db, session)
    return {
        "session": session,
        "message_count": msg_count,
        "summary": (sess["summary"][:200] if sess and sess.get("summary") else None),
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
    if entry is None or entry.task.done():
        return {"cancelled": False}

    plan = await get_plan_for_session(db, session)
    if plan is None or plan["status"] != "running":
        return {"cancelled": False}

    entry.cancel_event.set()
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
