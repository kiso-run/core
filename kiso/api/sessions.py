"""Session and message API routes."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import HTTPException
from pydantic import BaseModel
from starlette.responses import JSONResponse

import kiso.main as main_mod

router = APIRouter()


class SessionRequest(BaseModel):
    session: str
    webhook: str | None = None
    description: str | None = None


class MsgRequest(BaseModel):
    session: str
    user: str
    content: str


@router.post("/sessions")
async def post_sessions(
    body: SessionRequest,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    main_mod._validate_session_id(body.session)
    config = request.app.state.config
    if body.webhook:
        try:
            main_mod.validate_webhook_url(
                body.webhook,
                config.settings["webhook_allow_list"],
                require_https=main_mod.setting_bool(config.settings, "webhook_require_https"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    db = request.app.state.db
    _, created = await main_mod.upsert_session(
        db,
        body.session,
        connector=auth.token_name,
        webhook=body.webhook,
        description=body.description,
    )
    status_code = 201 if created else 200
    return JSONResponse(
        content={"session": body.session, "created": created},
        status_code=status_code,
    )


@router.post("/msg", status_code=202)
async def post_msg(
    body: MsgRequest,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    main_mod._validate_session_id(body.session)
    await main_mod._check_rate_limit(f"msg:{body.user}", limit=20)

    db = request.app.state.db
    config = request.app.state.config
    resolved = main_mod.resolve_user(config, body.user, auth.token_name)
    max_msg = main_mod.setting_int(config.settings, "max_message_size", lo=1)
    if len(body.content) > max_msg:
        raise HTTPException(status_code=413, detail="Message content too large")

    await main_mod.create_session(db, body.session, connector=auth.token_name)
    if not main_mod._is_admin(resolved):
        await main_mod._require_project_role(db, body.session, resolved.username, min_role="member")

    if resolved.trusted:
        msg_id = await main_mod.save_message(
            db,
            body.session,
            resolved.username,
            "user",
            body.content,
            trusted=True,
            processed=False,
        )
        user_role = resolved.user.role if resolved.user else "user"
        user_wrappers = resolved.user.wrappers if resolved.user else None
        msg_payload = {
            "id": msg_id,
            "content": body.content,
            "user_role": user_role,
            "user_wrappers": user_wrappers,
            "username": resolved.username,
            "base_url": str(request.base_url).rstrip("/"),
        }

        entry = main_mod._workers.get(body.session)
        worker_busy = (
            entry is not None
            and not entry.task.done()
            and main_mod._worker_phases.get(body.session, main_mod.WORKER_PHASE_IDLE)
            != main_mod.WORKER_PHASE_IDLE
        )

        if worker_busy:
            if main_mod.is_stop_message(body.content):
                main_mod.log.info("Fast-path stop detected: %r (session=%s)", body.content, body.session)
                entry.cancel_event.set()
                return {"queued": False, "session": body.session, "message_id": msg_id, "inflight": "stop"}

            plan = await main_mod.get_plan_for_session(db, body.session)
            plan_goal = (plan.get("goal", "") if plan else "") or ""
            inflight_recent = await main_mod.get_recent_messages(db, body.session, limit=3)
            inflight_ctx = main_mod.build_recent_context(inflight_recent, max_chars=400)
            category = await main_mod.run_inflight_classifier(
                config,
                plan_goal,
                body.content,
                session=body.session,
                recent_context=inflight_ctx,
            )
            if category == "stop":
                entry.cancel_event.set()
                return {"queued": False, "session": body.session, "message_id": msg_id, "inflight": "stop"}
            if category == "independent":
                entry.pending_messages.append(msg_payload)
                return {
                    "queued": False,
                    "session": body.session,
                    "message_id": msg_id,
                    "inflight": "independent",
                    "ack": "Got it — I'll handle this after the current job finishes.",
                }
            if category == "update":
                entry.update_hints.append(body.content)
                return {
                    "queued": False,
                    "session": body.session,
                    "message_id": msg_id,
                    "inflight": "update",
                    "ack": "Noted — will apply at the next step.",
                }
            if category == "conflict":
                entry.cancel_event.set()
                entry.pending_messages.insert(0, msg_payload)
                return {
                    "queued": False,
                    "session": body.session,
                    "message_id": msg_id,
                    "inflight": "conflict",
                    "ack": "Cancelling current job, starting new request.",
                }

        queue = main_mod._ensure_worker(body.session, db, config)
        try:
            queue.put_nowait(msg_payload)
        except asyncio.QueueFull:
            raise HTTPException(status_code=429, detail="Too many queued messages")
        return {"queued": True, "session": body.session, "message_id": msg_id}

    msg_id = await main_mod.save_message(
        db,
        body.session,
        resolved.username,
        "user",
        body.content,
        trusted=False,
        processed=True,
    )
    return {"queued": False, "session": body.session, "untrusted": True, "message_id": msg_id}


@router.get("/status/{session}")
async def get_status(
    session: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    user: str = Query(...),
    after: int = Query(0),
    verbose: bool = Query(False),
):
    db = request.app.state.db
    config = request.app.state.config
    resolved = main_mod.resolve_user(config, user, auth.token_name)
    is_admin = main_mod._is_admin(resolved)
    if not is_admin and not await main_mod.session_owned_by(db, session, resolved.username):
        raise HTTPException(status_code=403, detail="Access denied")
    if not is_admin:
        await main_mod._require_project_role(db, session, resolved.username, min_role="viewer")

    tasks = await main_mod.get_tasks_for_session(db, session, after=after)
    plan = await main_mod.get_plan_for_session(db, session)
    if not verbose:
        _strip_llm_verbose(tasks, plan)

    entry = main_mod._workers.get(session)
    worker_running = entry is not None and not entry.task.done()
    queue_length = entry.queue.qsize() if entry and not entry.task.done() else 0
    inflight = main_mod._llm_mod.get_inflight_call(session)
    if inflight and not verbose:
        inflight = {k: v for k, v in inflight.items() if k not in ("messages", "partial_content")}

    return {
        "tasks": tasks,
        "plan": plan,
        "queue_length": queue_length,
        "worker_running": worker_running,
        "active_task": None,
        "worker_phase": main_mod._worker_phases.get(session, main_mod.WORKER_PHASE_IDLE)
        if worker_running
        else main_mod.WORKER_PHASE_IDLE,
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
        for call in calls:
            call.pop("messages", None)
            call.pop("response", None)
        obj["llm_calls"] = json.dumps(calls)


@router.get("/sessions/{session}/info")
async def get_session_info(
    session: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    db = request.app.state.db
    msg_count = await main_mod.count_messages(db, session)
    sess = await main_mod.get_session(db, session)
    return {
        "session": session,
        "message_count": msg_count,
        "summary": (sess["summary"][:200] if sess and sess.get("summary") else None),
    }


@router.get("/sessions")
async def get_sessions(
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
    user: str = Query(...),
    all: bool = Query(False),
):
    db = request.app.state.db
    config = request.app.state.config
    resolved = main_mod.resolve_user(config, user, auth.token_name)
    if all and main_mod._is_admin(resolved):
        sessions = await main_mod.get_all_sessions(db)
    else:
        sessions = await main_mod.get_sessions_for_user(db, resolved.username)
    return sessions


@router.post("/sessions/{session}/cancel")
async def post_cancel(
    session: str,
    request: Request,
    auth: main_mod.AuthInfo = Depends(main_mod.require_auth),
):
    main_mod._validate_session_id(session)
    await main_mod._check_rate_limit(f"cancel:{session}", limit=20)

    db = request.app.state.db
    entry = main_mod._workers.get(session)
    if entry is None or entry.task.done():
        return {"cancelled": False}

    plan = await main_mod.get_plan_for_session(db, session)
    if plan is None or plan["status"] != "running":
        return {"cancelled": False}

    entry.cancel_event.set()
    drained_ids: list[int] = []
    while not entry.queue.empty():
        try:
            queued_msg = entry.queue.get_nowait()
            msg_id = queued_msg.get("id")
            if msg_id is not None:
                drained_ids.append(msg_id)
        except asyncio.QueueEmpty:
            break
    if drained_ids:
        await main_mod.mark_messages_processed_batch(db, drained_ids)
        main_mod.log.info("Cancel: drained %d queued messages for session=%s", len(drained_ids), session)

    return {"cancelled": True, "plan_id": plan["id"], "drained": len(drained_ids)}
