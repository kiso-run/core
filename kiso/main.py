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
from dataclasses import dataclass, field
from typing import NamedTuple, NoReturn

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.responses import JSONResponse

from kiso.auth import AuthInfo, ResolvedUser, require_auth, resolve_user
from kiso.stats import aggregate, read_audit_entries
from kiso.brain import (
    WORKER_PHASE_IDLE, invalidate_prompt_cache,
    classify_inflight, is_stop_message,
)
from kiso.config import ConfigError, KISO_DIR, load_config, reload_config, setting_bool, setting_int
import kiso.llm as _llm_mod
from kiso.log import setup_logging
from kiso.pub import pub_token, resolve_pub_token
from kiso.store import (
    count_messages,
    create_session,
    get_all_sessions,
    get_plan_for_session,
    get_safety_facts,
    list_knowledge,
    get_session,
    get_sessions_for_user,
    get_tasks_for_session,
    mark_messages_processed as mark_messages_processed_batch,
    save_fact,
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


def _is_admin(resolved: ResolvedUser) -> bool:
    """Check if the resolved user has admin privileges."""
    return bool(resolved.trusted and resolved.user and resolved.user.role == "admin")


def _raise_admin_required() -> NoReturn:
    raise HTTPException(status_code=403, detail="Admin access required")


async def _check_rate_limit(key: str, limit: int = 60) -> None:
    if not await _rate_limiter.check(key, limit=limit):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def _validate_session_id(session: str) -> None:
    if not SESSION_RE.match(session):
        raise HTTPException(status_code=400, detail="Invalid session ID")


async def _require_admin_with_ratelimit(request: Request, auth: AuthInfo, user: str) -> None:
    """Resolve user, enforce admin role, and apply admin rate limit."""
    config = request.app.state.config
    resolved = resolve_user(config, user, auth.token_name)
    if not _is_admin(resolved):
        _raise_admin_required()
    await _check_rate_limit(f"admin:{user}", limit=5)


@dataclass
class WorkerEntry:
    """Per-session worker state: queue, asyncio task, cancel event, and pending messages."""
    queue: asyncio.Queue
    task: asyncio.Task
    cancel_event: asyncio.Event
    pending_messages: list = field(default_factory=list)
    update_hints: list = field(default_factory=list)


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

    # --- SSH key generation (M355) ---
    _init_ssh_keys()


def _init_ssh_keys() -> None:
    """Generate SSH key pair if none exists, plus config and known_hosts."""
    import platform
    import subprocess

    ssh_dir = KISO_DIR / "sys" / "ssh"
    key_file = ssh_dir / "id_ed25519"
    if not key_file.exists():
        hostname = platform.node() or "kiso"
        try:
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-C", f"kiso@{hostname}",
                 "-f", str(key_file), "-N", ""],
                check=True, capture_output=True, timeout=30,
            )
            log.info("Generated SSH key pair at %s", key_file)
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired) as e:
            log.warning("Failed to generate SSH key: %s", e)

    # SSH config
    config_file = ssh_dir / "config"
    if not config_file.exists() and key_file.exists():
        config_file.write_text(
            "Host *\n"
            "  IdentityFile ~/.kiso/sys/ssh/id_ed25519\n"
            "  StrictHostKeyChecking accept-new\n"
            "  UserKnownHostsFile ~/.kiso/sys/ssh/known_hosts\n"
        )

    # known_hosts with common hosts
    known_hosts = ssh_dir / "known_hosts"
    if not known_hosts.exists() and key_file.exists():
        try:
            result = subprocess.run(
                ["ssh-keyscan", "github.com", "gitlab.com", "bitbucket.org"],
                capture_output=True, text=True, timeout=30,
            )
            if result.stdout:
                known_hosts.write_text(result.stdout)
                log.info("Populated known_hosts with common hosts")
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired) as e:
            log.warning("Failed to populate known_hosts: %s", e)


async def _collect_boot_facts(db) -> None:
    """Collect and store system self-knowledge facts at boot (M356)."""
    import platform as _plat

    from kiso.store import find_or_create_entity, save_fact

    entity_id = await find_or_create_entity(db, "self", "system")

    facts: list[tuple[str, list[str]]] = []  # (content, tags)

    # SSH public key
    pub_key = KISO_DIR / "sys" / "ssh" / "id_ed25519.pub"
    if pub_key.exists():
        key_text = pub_key.read_text().strip()
        facts.append((
            f"Instance SSH public key (at {pub_key}): {key_text}",
            ["ssh", "credentials"],
        ))

    # Hostname and user
    hostname = _plat.node() or "unknown"
    user = os.environ.get("USER", "root")
    facts.append((
        f"Instance runs as user '{user}' on host '{hostname}'",
        ["instance", "identity"],
    ))

    # Kiso version
    from importlib.metadata import version as _pkg_version
    try:
        _ver = _pkg_version("kiso")
    except Exception:
        _ver = "unknown"
    facts.append((f"Kiso version: {_ver}", ["instance", "version"]))

    for content, tags in facts:
        # Idempotent: skip if exact content already exists for this entity
        cur = await db.execute(
            "SELECT id FROM facts WHERE entity_id = ? AND content = ?",
            (entity_id, content),
        )
        if await cur.fetchone():
            continue
        await save_fact(
            db, content, source="system", session=None,
            category="system", tags=tags, entity_id=entity_id,
        )

    log.info("Boot facts: %d stored for entity 'self'", len(facts))


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
    pending: list = []
    hints: list = []
    task = asyncio.create_task(
        run_worker(db, config, session, queue, cancel_event=cancel_event,
                   set_phase=lambda phase, s=session: _set_worker_phase(s, phase),
                   pending_messages=pending, update_hints=hints)
    )

    def _cleanup(t, s=session):
        _workers.pop(s, None)
        _worker_phases.pop(s, None)

    task.add_done_callback(_cleanup)
    _workers[session] = WorkerEntry(queue, task, cancel_event, pending, hints)
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
            # Re-resolve user role/tools from current config
            resolved = resolve_user(config, msg["user"] or "", "")
            user_role = resolved.user.role if resolved.user else "user"
            user_tools = resolved.user.tools if resolved.user else None
            try:
                queue.put_nowait({
                    "id": msg["id"],
                    "content": msg["content"],
                    "user_role": user_role,
                    "user_tools": user_tools,
                    "username": msg["user"],
                    "base_url": "",
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


_CRON_CHECK_INTERVAL = 60  # seconds between cron checks


async def _cron_scheduler(db, config, app):
    """M679: Background loop that fires due cron jobs every 60 seconds."""
    from datetime import datetime

    from croniter import croniter

    from kiso.store import get_due_cron_jobs, update_cron_last_run

    while True:
        await asyncio.sleep(_CRON_CHECK_INTERVAL)
        try:
            now = datetime.now()
            now_iso = now.isoformat()
            due_jobs = await get_due_cron_jobs(db, now_iso)
            for job in due_jobs:
                session = job["session"]
                prompt = job["prompt"]
                log.info("Cron job %d fired: session=%s prompt=%r", job["id"], session, prompt[:80])

                # Save message and enqueue (same as POST /msg but internal)
                msg_id = await save_message(
                    db, session, "cron", "system", prompt,
                    trusted=True, processed=False, source="cron",
                )
                msg_payload = {
                    "id": msg_id,
                    "content": prompt,
                    "user_role": "admin",
                    "user_tools": "*",
                    "username": "cron",
                    "base_url": "",
                }
                queue = _ensure_worker(db, config, session)
                await queue.put(msg_payload)

                # Update next_run
                cron = croniter(job["schedule"], now)
                next_dt = cron.get_next(datetime)
                await update_cron_last_run(db, job["id"], now_iso, next_dt.isoformat())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Cron scheduler error (will retry next cycle)")


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

    await _collect_boot_facts(db)

    # M382: backfill entity_id for facts created before entity model
    from kiso.store import backfill_fact_entities
    backfilled = await backfill_fact_entities(db)
    if backfilled:
        log.info("Backfilled entity_id for %d orphan fact(s)", backfilled)

    await _startup_recovery(db, config)

    # Auto-repair unhealthy tools (re-run deps.sh for missing binaries)
    from kiso.tool_repair import repair_unhealthy_tools
    try:
        repaired = await repair_unhealthy_tools()
        if repaired:
            log.info("Repaired tools on startup: %s", repaired)
    except Exception as e:
        log.warning("Tool auto-repair failed: %s", e)

    # Webhook secret length warning
    webhook_secret = config.settings["webhook_secret"]
    if webhook_secret and len(webhook_secret) < 32:
        log.warning(
            "webhook_secret is only %d characters — recommend at least 32",
            len(webhook_secret),
        )

    # M679: Start cron scheduler background task
    cron_task = asyncio.create_task(_cron_scheduler(db, config, app))

    yield

    # Cancel cron scheduler
    cron_task.cancel()
    try:
        await cron_task
    except asyncio.CancelledError:
        pass

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
    from kiso.sysenv import get_resource_limits

    rl = get_resource_limits()
    max_disk = getattr(app.state, "config", None)
    if max_disk is not None:
        max_disk = max_disk.settings.get("max_disk_gb")
    if max_disk is None:
        max_disk = rl["disk_total_gb"]
    return {
        "status": "ok",
        "version": __version__,
        "build_hash": os.environ.get("KISO_BUILD_HASH", "dev"),
        "resources": {
            "memory_mb": {"used": rl["memory_used_mb"], "limit": rl["memory_mb"]},
            "cpu": {"limit": rl["cpu_limit"]},
            "disk_gb": {"used": rl["disk_used_gb"], "limit": max_disk},
            "pids": {"used": rl["pids_used"], "limit": rl["pids_limit"]},
        },
    }


@app.get("/pub/{token}/{filename:path}")
async def get_pub(token: str, filename: str, request: Request):
    """Serve a file from a session's pub/ directory. No authentication required."""
    client_ip = request.client.host if request.client else "unknown"
    await _check_rate_limit(f"pub:{client_ip}")

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
    response = FileResponse(path=file_path, filename=Path(filename).name, media_type=media_type)
    # M553: prevent XSS if HTML files are published
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/sessions")
async def post_sessions(
    body: SessionRequest,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    _validate_session_id(body.session)

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
    _validate_session_id(body.session)
    await _check_rate_limit(f"msg:{body.user}", limit=20)

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
        user_tools = resolved.user.tools if resolved.user else None

        msg_payload = {
            "id": msg_id,
            "content": body.content,
            "user_role": user_role,
            "user_tools": user_tools,
            "username": resolved.username,
            "base_url": str(request.base_url).rstrip("/"),
        }

        # --- In-flight message handling (M407/M408) ---
        entry = _workers.get(body.session)
        worker_busy = (
            entry is not None
            and not entry.task.done()
            and _worker_phases.get(body.session, WORKER_PHASE_IDLE) != WORKER_PHASE_IDLE
        )

        if worker_busy:
            # M407: fast-path stop detection
            if is_stop_message(body.content):
                log.info("Fast-path stop detected: %r (session=%s)", body.content, body.session)
                entry.cancel_event.set()
                return {"queued": False, "session": body.session, "message_id": msg_id,
                        "inflight": "stop"}

            # M408: classify in-flight message
            plan = await get_plan_for_session(db, body.session)
            plan_goal = (plan.get("goal", "") if plan else "") or ""
            category = await classify_inflight(
                config, plan_goal, body.content, session=body.session,
            )
            if category == "stop":
                entry.cancel_event.set()
                return {"queued": False, "session": body.session, "message_id": msg_id,
                        "inflight": "stop"}
            if category == "independent":
                entry.pending_messages.append(msg_payload)
                return {"queued": False, "session": body.session, "message_id": msg_id,
                        "inflight": "independent",
                        "ack": "Got it — I'll handle this after the current job finishes."}
            if category == "update":
                entry.update_hints.append(body.content)
                return {"queued": False, "session": body.session, "message_id": msg_id,
                        "inflight": "update",
                        "ack": "Noted — will apply at the next step."}
            if category == "conflict":
                entry.cancel_event.set()
                entry.pending_messages.insert(0, msg_payload)
                return {"queued": False, "session": body.session, "message_id": msg_id,
                        "inflight": "conflict",
                        "ack": "Cancelling current job, starting new request."}

        queue = _ensure_worker(body.session, db, config)
        try:
            queue.put_nowait(msg_payload)
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
    is_admin = _is_admin(resolved)
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
        inflight = {k: v for k, v in inflight.items() if k not in ("messages", "partial_content")}

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

    if all and _is_admin(resolved):
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
    _validate_session_id(session)
    await _check_rate_limit(f"cancel:{session}", limit=20)

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


# --- M413: Safety rules API ---


class SafetyRuleRequest(BaseModel):
    content: str


@app.get("/safety-rules")
async def list_safety_rules(
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    db = request.app.state.db
    facts = await get_safety_facts(db)
    return {"rules": facts}


@app.post("/safety-rules", status_code=201)
async def add_safety_rule(
    body: SafetyRuleRequest,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Rule content cannot be empty")

    fact_id = await save_fact(db, content, "admin", category="safety")
    return {"id": fact_id, "content": content}


@app.delete("/safety-rules/{rule_id}")
async def delete_safety_rule(
    rule_id: int,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db

    cur = await db.execute(
        "DELETE FROM facts WHERE id = ? AND category = 'safety'", (rule_id,),
    )
    await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Safety rule not found")

    return {"deleted": True, "id": rule_id}


# --- M672: Knowledge management endpoints ---


class KnowledgeRequest(BaseModel):
    content: str
    category: str = "general"
    entity_name: str | None = None
    entity_kind: str | None = None
    tags: list[str] | None = None


@app.get("/knowledge")
async def list_knowledge_endpoint(
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    category: str | None = None,
    entity: str | None = None,
    tag: str | None = None,
    search: str | None = None,
    limit: int = 50,
):
    db = request.app.state.db
    facts = await list_knowledge(
        db, category=category, entity=entity, tag=tag, search=search, limit=limit,
    )
    return {"facts": facts}


@app.post("/knowledge", status_code=201)
async def add_knowledge(
    body: KnowledgeRequest,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db

    from kiso.brain import _VALID_FACT_CATEGORIES
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if body.category not in _VALID_FACT_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category: {body.category}")

    entity_id = None
    if body.entity_name:
        from kiso.store import find_or_create_entity
        kind = body.entity_kind or "concept"
        entity_id = await find_or_create_entity(db, body.entity_name, kind)

    fact_id = await save_fact(
        db, content, "admin", category=body.category,
        tags=body.tags, entity_id=entity_id,
    )
    return {"id": fact_id, "content": content, "category": body.category}


@app.delete("/knowledge/{fact_id}")
async def delete_knowledge(
    fact_id: int,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db

    # Don't delete safety rules via this endpoint — use /safety-rules
    cur = await db.execute("SELECT category FROM facts WHERE id = ?", (fact_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Fact not found")
    if row["category"] == "safety":
        raise HTTPException(status_code=400, detail="Use /safety-rules to manage safety rules")

    await db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    await db.commit()
    return {"deleted": True, "id": fact_id}


# --- M680: Cron management endpoints ---


class CronRequest(BaseModel):
    session: str
    schedule: str
    prompt: str


@app.get("/cron")
async def list_cron(
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    session: str | None = None,
):
    db = request.app.state.db
    from kiso.store import list_cron_jobs
    jobs = await list_cron_jobs(db, session=session)
    return {"jobs": jobs}


@app.post("/cron", status_code=201)
async def create_cron(
    body: CronRequest,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db

    from croniter import croniter
    if not croniter.is_valid(body.schedule):
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {body.schedule}")

    # Verify session exists
    sess = await get_session(db, body.session)
    if not sess:
        await create_session(db, body.session)

    from datetime import datetime
    now = datetime.now()
    cron = croniter(body.schedule, now)
    next_run = cron.get_next(datetime).isoformat()

    from kiso.store import create_cron_job
    job_id = await create_cron_job(
        db, body.session, body.schedule, body.prompt, "admin", next_run,
    )
    return {"id": job_id, "session": body.session, "schedule": body.schedule, "next_run": next_run}


@app.delete("/cron/{job_id}")
async def delete_cron(
    job_id: int,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    from kiso.store import delete_cron_job
    deleted = await delete_cron_job(db, job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Cron job not found")
    return {"deleted": True, "id": job_id}


@app.patch("/cron/{job_id}")
async def update_cron(
    job_id: int,
    request: Request,
    auth: AuthInfo = Depends(require_auth),
    enabled: bool | None = None,
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    if enabled is None:
        raise HTTPException(status_code=400, detail="Must provide 'enabled' parameter")
    db = request.app.state.db
    from kiso.store import update_cron_enabled
    updated = await update_cron_enabled(db, job_id, enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Cron job not found")
    return {"id": job_id, "enabled": enabled}


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
    await _require_admin_with_ratelimit(request, auth, user)
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
    await _require_admin_with_ratelimit(request, auth, user)
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
    await _require_admin_with_ratelimit(request, auth, user)

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
