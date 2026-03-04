"""Tests for GET /status/{session} endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx

import kiso.main as main_mod
from kiso.main import WorkerEntry, app
from kiso.store import save_message, create_session
from tests.conftest import AUTH_HEADER


async def test_empty_session_status(client: httpx.AsyncClient):
    resp = await client.get("/status/empty-sess", params={"user": "testadmin"}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"] == []
    assert data["plan"] is None
    assert data["queue_length"] == 0
    assert data["worker_running"] is False
    assert data["active_task"] is None


async def test_status_after_param(client: httpx.AsyncClient):
    """?after= parameter is accepted and returns 200."""
    resp = await client.get("/status/some-sess", params={"user": "testadmin", "after": 999}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


async def test_status_no_auth(client: httpx.AsyncClient):
    resp = await client.get("/status/test", params={"user": "testadmin"})
    assert resp.status_code == 401


async def test_status_missing_user_param_returns_422(client: httpx.AsyncClient):
    """Missing required ?user= query param returns 422 Unprocessable Entity."""
    resp = await client.get("/status/test", headers=AUTH_HEADER)
    assert resp.status_code == 422


# ── 90c: ownership check ───────────────────────────────────────────────────────


async def test_status_non_admin_forbidden_on_unowned_session(client: httpx.AsyncClient):
    """Non-admin user gets 403 when they have no messages in the session."""
    resp = await client.get(
        "/status/stranger-sess",
        params={"user": "testuser"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 403
    assert "Access denied" in resp.json()["detail"]


async def test_status_non_admin_allowed_on_owned_session(client: httpx.AsyncClient):
    """Non-admin user can read status of a session where they have posted messages."""
    db = app.state.db
    await create_session(db, "owned-by-testuser")
    await save_message(db, "owned-by-testuser", "testuser", "user", "hello", trusted=True, processed=True)

    resp = await client.get(
        "/status/owned-by-testuser",
        params={"user": "testuser"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200


async def test_status_admin_allowed_on_any_session(client: httpx.AsyncClient):
    """Admin user can read status of any session, including ones they don't own."""
    resp = await client.get(
        "/status/some-random-session",
        params={"user": "testadmin"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200


# ── M109c: worker_phase and inflight_call fields ──────────────────────────────


async def test_status_includes_worker_phase_idle_no_worker(client: httpx.AsyncClient):
    """Without a running worker, worker_phase defaults to 'idle'."""
    resp = await client.get("/status/no-worker", params={"user": "testadmin"}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["worker_phase"] == "idle"


async def test_status_includes_worker_phase_from_running_worker(client: httpx.AsyncClient):
    """With a running worker whose phase is set, worker_phase reflects it."""
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_queue = asyncio.Queue()
    cancel_event = asyncio.Event()
    main_mod._workers["phase-sess"] = WorkerEntry(fake_queue, fake_task, cancel_event)
    main_mod._worker_phases["phase-sess"] = "planning"

    try:
        resp = await client.get(
            "/status/phase-sess",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["worker_phase"] == "planning"
    finally:
        main_mod._workers.pop("phase-sess", None)
        main_mod._worker_phases.pop("phase-sess", None)


async def test_status_includes_inflight_call(client: httpx.AsyncClient):
    """When an LLM call is inflight, it appears in status (verbose mode)."""
    import kiso.llm as llm_mod

    llm_mod._inflight_calls["inflight-sess"] = {
        "role": "planner",
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello"}],
        "ts": 12345.0,
    }
    try:
        resp = await client.get(
            "/status/inflight-sess",
            params={"user": "testadmin", "verbose": "true"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        inflight = resp.json()["inflight_call"]
        assert inflight["role"] == "planner"
        assert inflight["model"] == "gpt-4"
        assert inflight["messages"] is not None
    finally:
        llm_mod._inflight_calls.pop("inflight-sess", None)


async def test_status_inflight_strips_messages_when_not_verbose(client: httpx.AsyncClient):
    """Non-verbose status should omit messages from inflight_call."""
    import kiso.llm as llm_mod

    llm_mod._inflight_calls["strip-sess"] = {
        "role": "worker",
        "model": "gpt-3.5",
        "messages": [{"role": "user", "content": "hi"}],
        "ts": 99999.0,
    }
    try:
        resp = await client.get(
            "/status/strip-sess",
            params={"user": "testadmin"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        inflight = resp.json()["inflight_call"]
        assert inflight["role"] == "worker"
        assert "messages" not in inflight
    finally:
        llm_mod._inflight_calls.pop("strip-sess", None)
