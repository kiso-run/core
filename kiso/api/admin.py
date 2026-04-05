"""Admin and cron API routes."""

from __future__ import annotations

import os
from datetime import datetime as _dt, timedelta, timezone as _tz

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

import kiso.main as main_mod

router = APIRouter()


class CronRequest(BaseModel):
    session: str
    schedule: str
    prompt: str


@router.get("/cron")
async def list_cron(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    session: str | None = None,
):
    from kiso.store import list_cron_jobs

    jobs = await list_cron_jobs(request.app.state.db, session=session)
    return {"jobs": jobs}


@router.post("/cron", status_code=201)
async def create_cron(
    body: CronRequest,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    db = request.app.state.db
    from croniter import croniter

    if not croniter.is_valid(body.schedule):
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {body.schedule}")
    if not await main_mod.get_session(db, body.session):
        await main_mod.create_session(db, body.session)

    from datetime import datetime
    from kiso.store import create_cron_job

    now = datetime.now()
    cron = croniter(body.schedule, now)
    next_run = cron.get_next(datetime).isoformat()
    job_id = await create_cron_job(db, body.session, body.schedule, body.prompt, "admin", next_run)
    return {"id": job_id, "session": body.session, "schedule": body.schedule, "next_run": next_run}


@router.delete("/cron/{job_id}")
async def delete_cron(
    job_id: int,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    from kiso.store import delete_cron_job

    deleted = await delete_cron_job(request.app.state.db, job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Cron job not found")
    return {"deleted": True, "id": job_id}


@router.patch("/cron/{job_id}")
async def update_cron(
    job_id: int,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    enabled: bool | None = None,
):
    if auth.token_name != "cli":
        raise HTTPException(status_code=403, detail="Admin access required")
    if enabled is None:
        raise HTTPException(status_code=400, detail="Must provide 'enabled' parameter")
    from kiso.store import update_cron_enabled

    updated = await update_cron_enabled(request.app.state.db, job_id, enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Cron job not found")
    return {"id": job_id, "enabled": enabled}


@router.get("/admin/stats")
async def get_stats(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    user: str = Query(...),
    since: int = Query(30, description="Number of days to look back"),
    session: str | None = Query(None),
    by: str = Query("model"),
):
    await main_mod._require_admin_with_ratelimit(request, auth, user)
    if by not in ("model", "session", "role"):
        raise HTTPException(status_code=400, detail="by must be model, session, or role")

    since_dt = _dt.now(_tz.utc) - timedelta(days=since)
    entries = main_mod.read_audit_entries(main_mod.KISO_DIR / "audit", since=since_dt)
    if session:
        entries = [entry for entry in entries if entry.get("session") == session]

    rows = main_mod.aggregate(entries, by=by)
    total = {
        "calls": sum(row["calls"] for row in rows),
        "errors": sum(row["errors"] for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
    }
    return {
        "by": by,
        "since_days": since,
        "session_filter": session,
        "rows": rows,
        "total": total,
    }


@router.post("/admin/reload-config")
async def post_reload_config(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    user: str = Query(...),
):
    await main_mod._require_admin_with_ratelimit(request, auth, user)
    try:
        new_config = main_mod.reload_config()
        request.app.state.config = new_config
        main_mod.invalidate_prompt_cache()
        return {"reloaded": True}
    except main_mod.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/admin/reload-env")
async def post_reload_env(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    user: str = Query(...),
):
    await main_mod._require_admin_with_ratelimit(request, auth, user)

    env_vars = main_mod._load_env_file(main_mod.KISO_DIR / ".env")
    applied = 0
    skipped = 0
    for key, value in env_vars.items():
        if not any(key.startswith(prefix) for prefix in main_mod._ENV_KEY_PREFIXES):
            skipped += 1
            continue
        if len(value) > main_mod._ENV_VALUE_MAX_LEN or "\n" in value or "\r" in value:
            skipped += 1
            continue
        os.environ[key] = value
        main_mod.log.info("reload-env: applied %s", key)
        applied += 1

    main_mod.log.info("reload-env: %d applied, %d skipped", applied, skipped)
    return {"reloaded": True, "keys_applied": applied, "keys_skipped": skipped}
