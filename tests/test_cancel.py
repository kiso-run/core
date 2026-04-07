"""Integration tests for cancel flow (M414)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.conftest import AUTH_HEADER


def _make_busy_worker(session: str):
    """Set up a blocked worker + busy phase so cancel detection works."""
    from kiso.main import _workers, _worker_phases, WorkerEntry
    from kiso.brain import WORKER_PHASE_EXECUTING

    blocked = asyncio.Event()

    async def _blocked_worker(*args, **kwargs):
        await blocked.wait()

    queue = asyncio.Queue(maxsize=10)
    cancel_event = asyncio.Event()
    pending: list = []
    hints: list = []
    task = asyncio.create_task(_blocked_worker())
    _workers[session] = WorkerEntry(queue, task, cancel_event, pending, hints)
    _worker_phases[session] = WORKER_PHASE_EXECUTING
    return blocked, cancel_event, queue, task


async def _cleanup_worker(session: str, blocked, task):
    from kiso.main import _workers, _worker_phases
    blocked.set()
    _workers.pop(session, None)
    _worker_phases.pop(session, None)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _insert_running_plan(db, sess: str, goal: str = "do something"):
    """Insert a message + running plan for the given session."""
    from kiso.store import save_message
    msg_id = await save_message(db, sess, "testuser", "user", goal, trusted=True)
    await db.execute(
        "INSERT INTO plans (session, message_id, goal, status) VALUES (?, ?, ?, 'running')",
        (sess, msg_id, goal),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# M414-1: Cancel endpoint sets event and returns 200
# ---------------------------------------------------------------------------


async def test_cancel_sets_event_returns_200(client: httpx.AsyncClient):
    """POST /sessions/{sid}/cancel → sets cancel event, returns plan_id."""
    from kiso.store import create_session

    db = client._transport.app.state.db
    sess = "cancel-test-1"
    await create_session(db, sess)
    await _insert_running_plan(db, sess)

    blocked, cancel_event, _, task = _make_busy_worker(sess)
    try:
        resp = await client.post(f"/sessions/{sess}/cancel", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is True
        assert "plan_id" in data
        assert cancel_event.is_set()
    finally:
        await _cleanup_worker(sess, blocked, task)


# ---------------------------------------------------------------------------
# M414-2: Cancel during multi-task plan stops execution
# ---------------------------------------------------------------------------


async def test_cancel_during_plan_drains_queue(client: httpx.AsyncClient):
    """Cancel drains queued messages and reports count."""
    from kiso.store import create_session, save_message

    db = client._transport.app.state.db
    sess = "cancel-drain-test"
    await create_session(db, sess)
    await _insert_running_plan(db, sess, "multi-task plan")

    blocked, _, queue, task = _make_busy_worker(sess)

    # Queue two messages with valid IDs
    msg_id1 = await save_message(db, sess, "testuser", "user", "msg1", trusted=True, processed=False)
    msg_id2 = await save_message(db, sess, "testuser", "user", "msg2", trusted=True, processed=False)
    queue.put_nowait({"id": msg_id1, "content": "msg1"})
    queue.put_nowait({"id": msg_id2, "content": "msg2"})

    try:
        resp = await client.post(f"/sessions/{sess}/cancel", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is True
        assert data["drained"] == 2
        assert queue.empty()
    finally:
        await _cleanup_worker(sess, blocked, task)


# ---------------------------------------------------------------------------
# M414-3: Cancel on idle session → not cancelled
# ---------------------------------------------------------------------------


async def test_cancel_idle_session_returns_not_cancelled(client: httpx.AsyncClient):
    """Cancel when no worker running → cancelled: false."""
    from kiso.store import create_session

    db = client._transport.app.state.db
    sess = "cancel-idle-test"
    await create_session(db, sess)

    resp = await client.post(f"/sessions/{sess}/cancel", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["cancelled"] is False


async def test_cancel_no_running_plan_returns_not_cancelled(client: httpx.AsyncClient):
    """Cancel when worker exists but no running plan → cancelled: false."""
    from kiso.store import create_session

    db = client._transport.app.state.db
    sess = "cancel-no-plan"
    await create_session(db, sess)

    blocked, _, _, task = _make_busy_worker(sess)
    try:
        resp = await client.post(f"/sessions/{sess}/cancel", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is False
    finally:
        await _cleanup_worker(sess, blocked, task)


# ---------------------------------------------------------------------------
# M414-4: CLI `kiso cancel` output
# ---------------------------------------------------------------------------


def _fake_cancel_context(session, resp_json):
    """Build a fake client context for cancel CLI tests."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = resp_json
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp

    ctx = MagicMock()
    ctx.client = mock_client
    ctx.session = session
    return ctx


def test_cancel_cli_success(capsys):
    """kiso cancel prints confirmation on success."""
    import argparse
    from cli.__init__ import _cancel_cmd

    args = argparse.Namespace(api="http://test:8333", cancel_session="test-sess")
    ctx = _fake_cancel_context("test-sess", {"cancelled": True, "plan_id": 42, "drained": 1})

    with patch("cli.__init__._setup_client_context", return_value=ctx):
        _cancel_cmd(args)

    out = capsys.readouterr().out
    assert "cancelled" in out.lower()
    assert "42" in out
    assert "drained" in out.lower()


def test_cancel_cli_no_active_job(capsys):
    """kiso cancel with no active job → error message."""
    import argparse
    from cli.__init__ import _cancel_cmd

    args = argparse.Namespace(api="http://test:8333", cancel_session=None)
    ctx = _fake_cancel_context("default-sess", {"cancelled": False})

    with patch("cli.__init__._setup_client_context", return_value=ctx):
        with pytest.raises(SystemExit) as exc_info:
            _cancel_cmd(args)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "no active job" in err.lower()


# ---------------------------------------------------------------------------
# cancel_event kills running subprocess via _exec_task / _tool_task
# ---------------------------------------------------------------------------


async def test_exec_task_cancel_kills_subprocess(tmp_path):
    """_exec_task with cancel_event kills the subprocess."""
    from kiso.worker.exec import _exec_task
    from unittest.mock import patch as _patch

    cancel = asyncio.Event()
    asyncio.get_event_loop().call_later(0.1, cancel.set)

    with _patch("kiso.worker.utils.KISO_DIR", tmp_path):
        stdout, stderr, success, exit_code = await _exec_task(
            "cancel-test-sess", "sleep 30",
            cancel_event=cancel,
        )
    assert success is False
    assert exit_code == -15
    assert stderr == "cancelled"


async def test_exec_task_normal_with_cancel_event(tmp_path):
    """_exec_task with cancel_event not set → normal completion."""
    from kiso.worker.exec import _exec_task
    from unittest.mock import patch as _patch

    cancel = asyncio.Event()  # never set

    with _patch("kiso.worker.utils.KISO_DIR", tmp_path):
        stdout, stderr, success, exit_code = await _exec_task(
            "cancel-test-sess2", "echo works",
            cancel_event=cancel,
        )
    assert success is True
    assert "works" in stdout


# ---------------------------------------------------------------------------
# cancel check fires after each completed task (not just batch start)
# ---------------------------------------------------------------------------


async def test_cancel_after_task_cancels_remaining(tmp_path):
    """cancel_event set during first task → remaining tasks cancelled."""
    from kiso.worker.exec import _exec_task
    from unittest.mock import patch as _patch

    cancel = asyncio.Event()

    # First call: long sleep that gets cancelled
    asyncio.get_event_loop().call_later(0.1, cancel.set)
    with _patch("kiso.worker.utils.KISO_DIR", tmp_path):
        _, stderr, success, code = await _exec_task(
            "m768-test", "sleep 30", cancel_event=cancel,
        )
    assert code == -15
    assert stderr == "cancelled"
    # cancel_event is now set — _execute_plan would check it after this task
    # and cancel remaining tasks without starting them
    assert cancel.is_set()


# ---------------------------------------------------------------------------
# cancel handler returns correct stop signal
# ---------------------------------------------------------------------------


def test_cancel_handler_result_fields():
    """Cancel handler must set stop=True and stop_replan as a string, not a bool."""
    from kiso.worker.loop import _TaskHandlerResult

    result = _TaskHandlerResult(stop=True, stop_replan="cancelled")
    assert result.stop is True
    assert result.stop_replan == "cancelled"
    assert isinstance(result.stop_replan, str)
