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
    _VALID_FACT_CATEGORIES,
    build_recent_context, run_inflight_classifier, is_stop_message,
)
from kiso.config import ConfigError, KISO_DIR, load_config, reload_config, setting_bool, setting_int
import kiso.llm as _llm_mod
from kiso.log import setup_logging
from kiso.pub import pub_token, resolve_pub_token
from kiso.store import (
    add_project_member,
    bind_session_to_project,
    count_messages,
    create_project,
    create_session,
    delete_project,
    get_all_sessions,
    get_plan_for_session,
    get_project,
    get_recent_messages,
    get_safety_facts,
    get_session_project_id,
    get_user_project_role,
    list_knowledge,
    list_project_members,
    list_projects,
    get_session,
    get_sessions_for_user,
    get_tasks_for_session,
    mark_messages_processed as mark_messages_processed_batch,
    remove_project_member,
    save_fact,
    session_owned_by,
    get_unprocessed_trusted_messages,
    init_db,
    recover_stale_running,
    save_message,
    unbind_session_from_project,
    upsert_session,
)
from kiso.webhook import validate_webhook_url
from kiso.worker import run_worker
from kiso.api import (
    admin_router,
    knowledge_router,
    projects_router,
    runtime_router,
    sessions_router,
)
from kiso.api.sessions import _strip_llm_verbose

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


async def _require_project_role(
    db, session: str, username: str, *, min_role: str = "viewer",
) -> None:
    """Raise 403 if session has a project and user lacks *min_role*.

    min_role="viewer" allows both viewer and member.
    min_role="member" requires member role.
    """
    project_id = await get_session_project_id(db, session)
    if project_id is None:
        return  # No project attached — no restriction
    role = await get_user_project_role(db, project_id, username)
    if role is None:
        raise HTTPException(status_code=403, detail="Not a member of this project")
    if min_role == "member" and role != "member":
        raise HTTPException(status_code=403, detail="Project member role required")


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


def _migrate_summarizer_session_role(roles_dir: "Path") -> None:
    """In-place migration for the M1293 summarizer file rename.

    The bundled file ``summarizer-session.md`` was renamed to
    ``summarizer.md`` so the role filename matches its key in
    ``_MODEL_METADATA``. Existing user dirs may still hold a
    customized ``summarizer-session.md`` from before M1293; this
    helper renames it in place.

    Behavior:

    - Only ``summarizer-session.md`` exists → rename to
      ``summarizer.md``.
    - Only ``summarizer.md`` exists → no-op.
    - Both exist → keep both, log a warning, and let the new
      filename win at load time (the runtime loader looks up
      ``summarizer.md``).
    - Neither exists → no-op.

    Idempotent: safe to call on every boot.
    """
    legacy = roles_dir / "summarizer-session.md"
    new = roles_dir / "summarizer.md"
    if not legacy.exists():
        return
    if new.exists():
        log.warning(
            "Both summarizer-session.md and summarizer.md exist in %s; "
            "keeping both, summarizer.md is the canonical one",
            roles_dir,
        )
        return
    try:
        legacy.rename(new)
        log.info("Migrated %s -> summarizer.md", legacy.name)
    except OSError as e:
        log.warning("Failed to migrate summarizer-session.md: %s", e)


def _populate_kiso_dir(target: Path) -> None:
    """Seed *target* with standard subdirs + bundled roles + reference docs.

    Called by :func:`_init_kiso_dirs` at server startup with
    ``target=KISO_DIR``. Idempotent and safe to call repeatedly.
    Does NOT generate SSH keys or fetch boot facts — those stay
    in :func:`_init_kiso_dirs` because they are production-only.

    Behavior:

    - Creates the standard subdirs (``wrappers``, ``connectors``,
      ``recipes``, ``sessions``, ``roles``, ``reference``,
      ``sys/bin``, ``sys/ssh``).
    - Runs the M1293 ``summarizer-session.md → summarizer.md``
      migration before copying bundled roles, so a stale legacy
      file does not land alongside the canonical filename.
    - Additively copies bundled roles into ``target/roles/``: a
      file is written only if missing or empty (catches
      ``> file.md`` accidents). Existing non-empty user files
      are never overwritten.
    - Syncs bundled reference docs into ``target/reference/``,
      overwriting on content change.

    The eager seed performed here mirrors the lazy self-heal in
    :func:`kiso.brain.prompts._load_system_prompt`. Both paths
    converge on the same end state: the user dir is the runtime
    source of truth, and the bundled defaults are the factory
    seed. The eager path lets ``ls ~/.kiso/roles/`` work right
    after install; the lazy path makes the loader resilient to
    runtime corruption.
    """
    import importlib.resources

    try:
        (target / "sys" / "bin").mkdir(parents=True, exist_ok=True)
        (target / "sys" / "ssh").mkdir(parents=True, exist_ok=True)
        (target / "reference").mkdir(parents=True, exist_ok=True)
        (target / "wrappers").mkdir(parents=True, exist_ok=True)
        (target / "connectors").mkdir(parents=True, exist_ok=True)
        (target / "recipes").mkdir(parents=True, exist_ok=True)
        (target / "sessions").mkdir(parents=True, exist_ok=True)
        (target / "roles").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Failed to create kiso directories under %s: %s", target, e)
        return

    # Sync bundled reference docs
    try:
        ref_pkg = importlib.resources.files("kiso") / "reference"
        dest = target / "reference"
        for src_file in ref_pkg.iterdir():
            if src_file.name.endswith(".md"):
                ref_target = dest / src_file.name
                try:
                    content = src_file.read_text(encoding="utf-8")
                    if not ref_target.exists() or ref_target.read_text(encoding="utf-8") != content:
                        ref_target.write_text(content, encoding="utf-8")
                except OSError as e:
                    log.warning("Failed to sync reference file %s: %s", src_file.name, e)
    except (FileNotFoundError, OSError, TypeError) as e:
        log.warning("Failed to sync reference docs: %s", e)

    # M1293: rename legacy summarizer-session.md → summarizer.md before
    # the additive copy, so the bundled summarizer.md does not land
    # alongside a stale legacy file.
    _migrate_summarizer_session_role(target / "roles")

    # Additively copy bundled roles. Existing non-empty user files
    # are preserved; empty files are self-healed.
    try:
        roles_pkg = importlib.resources.files("kiso") / "roles"
        roles_dest = target / "roles"
        for src_file in roles_pkg.iterdir():
            if not src_file.name.endswith(".md"):
                continue
            role_target = roles_dest / src_file.name
            try:
                if not role_target.exists() or role_target.stat().st_size == 0:
                    role_target.write_text(
                        src_file.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
            except OSError as e:
                log.warning("Failed to copy role file %s: %s", src_file.name, e)
    except (FileNotFoundError, OSError, TypeError) as e:
        log.warning("Failed to copy bundled roles: %s", e)


def _init_kiso_dirs() -> None:
    """Ensure ~/.kiso/ subdirectories exist and sync bundled defaults.

    Server-startup wrapper around :func:`_populate_kiso_dir` plus
    SSH key generation. The role population is **eager** here so
    that ``ls ~/.kiso/roles/`` shows files immediately after install
    and operators can edit them without waiting for the first LLM
    call. The runtime loader
    :func:`kiso.brain.prompts._load_system_prompt` performs a
    matching **lazy** self-heal on first access for any file that
    is missing or empty after startup (e.g., a runtime corruption).
    """
    _populate_kiso_dir(KISO_DIR)
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
    """Collect and store system self-knowledge facts at boot."""
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
            # Re-resolve user role/wrappers from current config
            resolved = resolve_user(config, msg["user"] or "", "")
            user_role = resolved.user.role if resolved.user else "user"
            user_wrappers = resolved.user.wrappers if resolved.user else None
            try:
                queue.put_nowait({
                    "id": msg["id"],
                    "content": msg["content"],
                    "user_role": user_role,
                    "user_wrappers": user_wrappers,
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
    """Background loop that fires due cron jobs every 60 seconds."""
    from datetime import datetime

    from croniter import croniter

    from kiso.store import get_due_cron_jobs, update_cron_last_run

    while True:
        await asyncio.sleep(_CRON_CHECK_INTERVAL)
        try:
            now = datetime.now()
            now_iso = now.isoformat()
            due_jobs = await get_due_cron_jobs(db, now_iso)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Cron scheduler: failed to fetch due jobs")
            continue

        for job in due_jobs:
            try:
                session = job["session"]
                prompt = job["prompt"]
                log.info("Cron job %d fired: session=%s prompt=%r", job["id"], session, prompt[:80])

                msg_id = await save_message(
                    db, session, "cron", "system", prompt,
                    trusted=True, processed=False, source="cron",
                )
                msg_payload = {
                    "id": msg_id,
                    "content": prompt,
                    "user_role": "admin",
                    "user_wrappers": "*",
                    "username": "cron",
                    "base_url": "",
                }
                queue = _ensure_worker(session, db, config)
                await queue.put(msg_payload)

                cron = croniter(job["schedule"], now)
                next_dt = cron.get_next(datetime)
                await update_cron_last_run(db, job["id"], now_iso, next_dt.isoformat())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Cron job %d failed (will retry next cycle)", job["id"])


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

    # backfill entity_id for facts created before entity model
    from kiso.store import backfill_fact_entities
    backfilled = await backfill_fact_entities(db)
    if backfilled:
        log.info("Backfilled entity_id for %d orphan fact(s)", backfilled)

    await _startup_recovery(db, config)

    # Wrapper deps repair runs in background — doesn't block server healthcheck
    from kiso.wrapper_repair import _is_container_rebuilt, _mark_image_id, rerun_all_deps, repair_unhealthy_wrappers

    async def _background_wrapper_repair():
        try:
            if _is_container_rebuilt():
                log.info("Container rebuilt — re-running deps.sh in background...")
                reran = await rerun_all_deps()
                if reran:
                    log.info("Re-ran deps.sh for: %s", reran)
                _mark_image_id()
            repaired = await repair_unhealthy_wrappers()
            if repaired:
                log.info("Repaired wrappers on startup: %s", repaired)
        except Exception as e:
            log.warning("Background wrapper repair failed: %s", e)

    repair_task = asyncio.create_task(_background_wrapper_repair())

    # Webhook secret length warning
    webhook_secret = config.settings["webhook_secret"]
    if webhook_secret and len(webhook_secret) < 32:
        log.warning(
            "webhook_secret is only %d characters — recommend at least 32",
            len(webhook_secret),
        )

    # Start cron scheduler background task
    cron_task = asyncio.create_task(_cron_scheduler(db, config, app))

    yield

    # Cancel background tasks
    for task in (cron_task, repair_task):
        task.cancel()
        try:
            await task
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
app.include_router(runtime_router)
app.include_router(sessions_router)
app.include_router(knowledge_router)
app.include_router(projects_router)
app.include_router(admin_router)
