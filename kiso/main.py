"""FastAPI application."""

from __future__ import annotations

import asyncio
from datetime import datetime as _dt, timedelta, timezone as _tz
import json
import logging
import mimetypes
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.responses import JSONResponse

from kiso.auth import AuthInfo, require_auth, resolve_user
from kiso.stats import aggregate, read_audit_entries
from kiso.brain import WORKER_PHASE_IDLE, invalidate_prompt_cache
from kiso.config import ConfigError, KISO_DIR, load_config, reload_config, setting_bool, setting_int
import kiso.llm as _llm_mod
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
    mark_messages_processed as mark_messages_processed_batch,
    session_owned_by,
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

# Allowed prefixes for /admin/reload-env; all other keys are skipped.
_ENV_KEY_PREFIXES = ("KISO_", "OPENAI_", "ANTHROPIC_", "OLLAMA_")
_ENV_VALUE_MAX_LEN = 1024


class _RateLimiter:
    """Token-bucket rate limiter backed by asyncio.Lock (no external deps)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # key → (tokens, last_refill_monotonic)
        self._buckets: dict[str, tuple[float, float]] = {}

    def reset(self) -> None:
        """Clear all bucket state (used in tests)."""
        self._buckets.clear()

    async def check(self, key: str, limit: int, window: float = 60.0) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        async with self._lock:
            tokens, last = self._buckets.get(key, (float(limit), now))
            elapsed = now - last
            tokens = min(float(limit), tokens + elapsed * (limit / window))
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            tokens -= 1.0
            self._buckets[key] = (tokens, now)
            return True


_rate_limiter = _RateLimiter()


class WorkerEntry(NamedTuple):
    """Per-session worker state: queue, asyncio task, and cancel event."""
    queue: asyncio.Queue
    task: asyncio.Task
    cancel_event: asyncio.Event


def _init_kiso_dirs() -> None:
    """Ensure ~/.kiso/ subdirectories exist and sync reference docs."""
    try:
        (KISO_DIR / "sys" / "bin").mkdir(parents=True, exist_ok=True)
        (KISO_DIR / "sys" / "ssh").mkdir(parents=True, exist_ok=True)
        (KISO_DIR / "reference").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Failed to create kiso directories: %s", e)
        return

    # Sync bundled reference docs to ~/.kiso/reference/
    import importlib.resources
    try:
        ref_pkg = importlib.resources.files("kiso") / "reference"
        dest = KISO_DIR / "reference"
        for src_file in ref_pkg.iterdir():
            if src_file.name.endswith(".md"):
                target = dest / src_file.name
                try:
                    content = src_file.read_text(encoding="utf-8")
                    # Only write if changed (avoid unnecessary writes)
                    if not target.exists() or target.read_text(encoding="utf-8") != content:
                        target.write_text(content, encoding="utf-8")
                except OSError as e:
                    log.warning("Failed to sync reference file %s: %s", src_file.name, e)
    except (FileNotFoundError, OSError, TypeError) as e:
        log.warning("Failed to sync reference docs: %s", e)

# Per-session workers: session → WorkerEntry
_workers: dict[str, WorkerEntry] = {}

# Per-session worker phase: session → phase string (e.g. "classifying", "planning", "executing", "idle")
_worker_phases: dict[str, str] = {}


def _set_worker_phase(session: str, phase: str) -> None:
    """Set the current worker phase for a session (injected as callback into run_worker)."""
    _worker_phases[session] = phase


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
    maxsize = setting_int(config.settings, "max_queue_size", lo=1)
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        run_worker(db, config, session, queue, cancel_event=cancel_event,
                   set_phase=lambda phase, s=session: _set_worker_phase(s, phase))
    )

    def _cleanup(t, s=session):
        _workers.pop(s, None)
        _worker_phases.pop(s, None)

    task.add_done_callback(_cleanup)
    _workers[session] = WorkerEntry(queue, task, cancel_event)
    return queue


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Skip comments and blank lines."""
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _init_app_state(app: FastAPI, config, db) -> None:
    """Set minimal app state. Called from lifespan and test fixtures."""
    app.state.config = config
    app.state.db = db


async def _startup_recovery(db, config) -> None:
    """Mark stale running plans/tasks as failed and re-enqueue unprocessed messages."""
    from collections import defaultdict

    # Mark stale running plans/tasks as failed
    plans_recovered, tasks_recovered = await recover_stale_running(db)
    if plans_recovered or tasks_recovered:
        log.info(
            "Startup recovery: %d stale plans, %d stale tasks marked failed",
            plans_recovered, tasks_recovered,
        )

    # Re-enqueue unprocessed trusted messages
    unprocessed = await get_unprocessed_trusted_messages(db)
    if not unprocessed:
        return

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
                    "Queue full for session=%s during startup recovery — "
                    "message %d remains unprocessed in DB and will retry on next restart",
                    sess_id, msg["id"],
                )
    if recovered_count:
        log.info("Startup recovery: re-enqueued %d unprocessed messages", recovered_count)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    config = load_config()
    _init_kiso_dirs()
    await _llm_mod.init_http_client(timeout=setting_int(config.settings, "llm_timeout", lo=1))
    log.info("Server starting — host=%s port=%s",
             config.settings["host"],
             config.settings["port"])
    db = await init_db(KISO_DIR / "store.db")
    _init_app_state(app, config, db)

    await _startup_recovery(db, config)

    # Auto-repair unhealthy skills (re-run deps.sh for missing binaries)
    from kiso.skill_repair import repair_unhealthy_skills
    try:
        repaired = await repair_unhealthy_skills()
        if repaired:
            log.info("Repaired skills on startup: %s", repaired)
    except Exception as e:
        log.warning("Skill auto-repair failed: %s", e)

    # Webhook secret length warning
    webhook_secret = config.settings["webhook_secret"]
    if webhook_secret and len(webhook_secret) < 32:
        log.warning(
            "webhook_secret is only %d characters — recommend at least 32",
            len(webhook_secret),
        )

    yield

    # Graceful shutdown with timeout
    shutdown_timeout = setting_int(config.settings, "llm_timeout", lo=1)
    for session, entry in list(_workers.items()):
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
    await _llm_mod.close_http_client()
    await app.state.db.close()
    log.info("Server shut down")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    from kiso._version import __version__
    return {
        "status": "ok",
        "version": __version__,
        "build_hash": os.environ.get("KISO_BUILD_HASH", "dev"),
    }


@app.get("/pub/{token}/{filename:path}")
async def get_pub(token: str, filename: str, request: Request):
    """Serve a file from a session's pub/ directory. No authentication required."""
    client_ip = request.client.host if request.client else "unknown"
    if not await _rate_limiter.check(f"pub:{client_ip}", limit=60):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    config = request.app.state.config
    session = resolve_pub_token(token, config)
    if session is None:
        raise HTTPException(status_code=404, detail="Not found")

    pub_dir = KISO_DIR / "sessions" / session / "pub"
    file_path = (pub_dir / filename).resolve()

    # Path traversal guard — is_relative_to is immune to same-prefix sibling dirs
    if not file_path.is_relative_to(pub_dir.resolve()):
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
                config.settings["webhook_allow_list"],
                require_https=setting_bool(config.settings, "webhook_require_https"),
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

    if not await _rate_limiter.check(f"msg:{body.user}", limit=20):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    db = request.app.state.db
    config = request.app.state.config
    resolved = resolve_user(config, body.user, auth.token_name)

    max_msg = setting_int(config.settings, "max_message_size", lo=1)
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
    user: str = Query(...),
    after: int = Query(0),
    verbose: bool = Query(False),
):
    db = request.app.state.db
    config = request.app.state.config
    resolved = resolve_user(config, user, auth.token_name)
    is_admin = resolved.trusted and resolved.user and resolved.user.role == "admin"
    if not is_admin and not await session_owned_by(db, session, resolved.username):
        raise HTTPException(status_code=403, detail="Access denied")

    tasks = await get_tasks_for_session(db, session, after=after)
    plan = await get_plan_for_session(db, session)

    if not verbose:
        _strip_llm_verbose(tasks, plan)

    entry = _workers.get(session)
    worker_running = entry is not None and not entry.task.done()
    queue_length = entry.queue.qsize() if entry and not entry.task.done() else 0

    # Inflight LLM call (live call in progress)
    inflight = _llm_mod.get_inflight_call(session)
    if inflight and not verbose:
        inflight = {k: v for k, v in inflight.items() if k != "messages"}

    return {
        "tasks": tasks,
        "plan": plan,
        "queue_length": queue_length,
        "worker_running": worker_running,
        "active_task": None,
        "worker_phase": _worker_phases.get(session, WORKER_PHASE_IDLE) if worker_running else WORKER_PHASE_IDLE,
        "inflight_call": inflight,
    }


def _strip_llm_verbose(tasks: list[dict], plan: dict | None) -> None:
    """Remove messages/response from llm_calls to keep default response compact."""
    for obj in ([plan] if plan else []) + tasks:
        raw = obj.get("llm_calls")
        if not raw:
            continue
        try:
            calls = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for c in calls:
            c.pop("messages", None)
            c.pop("response", None)
        obj["llm_calls"] = json.dumps(calls)


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

    if not await _rate_limiter.check(f"cancel:{session}", limit=20):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    db = request.app.state.db

    entry = _workers.get(session)
    if entry is None or entry.task.done():
        return {"cancelled": False}

    plan = await get_plan_for_session(db, session)
    if plan is None or plan["status"] != "running":
        return {"cancelled": False}

    entry.cancel_event.set()

    # Drain pending queue so subsequent messages aren't blocked behind stale ones
    drained_ids: list[int] = []
    while not entry.queue.empty():
        try:
            queued_msg = entry.queue.get_nowait()
            mid = queued_msg.get("id")
            if mid is not None:
                drained_ids.append(mid)
        except asyncio.QueueEmpty:
            break
    if drained_ids:
        await mark_messages_processed_batch(db, drained_ids)
        log.info("Cancel: drained %d queued messages for session=%s", len(drained_ids), session)

    return {"cancelled": True, "plan_id": plan["id"], "drained": len(drained_ids)}


@app.get("/admin/stats")
async def get_stats(
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    user: str = Query(...),
    since: int = Query(30, description="Number of days to look back"),
    session: str | None = Query(None),
    by: str = Query("model"),
):
    """Return aggregated token-usage stats from the audit log (admin only).

    *by* must be one of ``model``, ``session``, or ``role``.
    """
    config = request.app.state.config
    resolved = resolve_user(config, user, auth.token_name)
    if not resolved.trusted or not resolved.user or resolved.user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if not await _rate_limiter.check(f"admin:{user}", limit=5):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if by not in ("model", "session", "role"):
        raise HTTPException(status_code=400, detail="by must be model, session, or role")

    since_dt = _dt.now(_tz.utc) - timedelta(days=since)
    entries = read_audit_entries(KISO_DIR / "audit", since=since_dt)
    if session:
        entries = [e for e in entries if e.get("session") == session]

    rows = aggregate(entries, by=by)
    total = {
        "calls": sum(r["calls"] for r in rows),
        "errors": sum(r["errors"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }
    return {
        "by": by,
        "since_days": since,
        "session_filter": session,
        "rows": rows,
        "total": total,
    }


@app.post("/admin/reload-config")
async def post_reload_config(
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    user: str = Query(...),
):
    config = request.app.state.config
    resolved = resolve_user(config, user, auth.token_name)
    if not resolved.trusted or not resolved.user or resolved.user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if not await _rate_limiter.check(f"admin:{user}", limit=5):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    try:
        new_config = reload_config()
        request.app.state.config = new_config
        invalidate_prompt_cache()
        return {"reloaded": True}
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    if not await _rate_limiter.check(f"admin:{user}", limit=5):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    env_vars = _load_env_file(KISO_DIR / ".env")
    applied = 0
    skipped = 0
    for key, value in env_vars.items():
        if not any(key.startswith(p) for p in _ENV_KEY_PREFIXES):
            skipped += 1
            continue
        if len(value) > _ENV_VALUE_MAX_LEN or "\n" in value or "\r" in value:
            skipped += 1
            continue
        os.environ[key] = value
        log.info("reload-env: applied %s", key)
        applied += 1

    log.info("reload-env: %d applied, %d skipped", applied, skipped)
    return {"reloaded": True, "keys_applied": applied, "keys_skipped": skipped}
