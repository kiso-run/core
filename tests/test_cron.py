"""M678-M682: Tests for cron job management."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kiso.store import (
    create_cron_job,
    create_session,
    delete_cron_job,
    get_due_cron_jobs,
    init_db,
    list_cron_jobs,
    update_cron_enabled,
    update_cron_last_run,
)


@pytest.fixture()
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    await create_session(conn, "sess1")
    await create_session(conn, "sess2")
    yield conn
    await conn.close()


# --- M678: Store functions ---


async def test_create_cron_job(db):
    job_id = await create_cron_job(
        db, "sess1", "0 9 * * *", "check prices", "admin", "2026-03-18T09:00:00",
    )
    assert isinstance(job_id, int)
    assert job_id > 0


async def test_list_cron_jobs_all(db):
    await create_cron_job(db, "sess1", "0 9 * * *", "job A", "admin", "2026-03-18T09:00:00")
    await create_cron_job(db, "sess2", "*/5 * * * *", "job B", "admin", "2026-03-17T12:05:00")
    jobs = await list_cron_jobs(db)
    assert len(jobs) == 2


async def test_list_cron_jobs_by_session(db):
    await create_cron_job(db, "sess1", "0 9 * * *", "job A", "admin", "2026-03-18T09:00:00")
    await create_cron_job(db, "sess2", "*/5 * * * *", "job B", "admin", "2026-03-17T12:05:00")
    jobs = await list_cron_jobs(db, session="sess1")
    assert len(jobs) == 1
    assert jobs[0]["prompt"] == "job A"


async def test_delete_cron_job(db):
    job_id = await create_cron_job(db, "sess1", "0 9 * * *", "job A", "admin", "2026-03-18T09:00:00")
    assert await delete_cron_job(db, job_id) is True
    assert await list_cron_jobs(db) == []


async def test_delete_nonexistent_cron_job(db):
    assert await delete_cron_job(db, 99999) is False


async def test_update_cron_enabled(db):
    job_id = await create_cron_job(db, "sess1", "0 9 * * *", "job A", "admin", "2026-03-18T09:00:00")
    await update_cron_enabled(db, job_id, False)
    jobs = await list_cron_jobs(db)
    assert jobs[0]["enabled"] == 0

    await update_cron_enabled(db, job_id, True)
    jobs = await list_cron_jobs(db)
    assert jobs[0]["enabled"] == 1


async def test_get_due_cron_jobs(db):
    # Job due in the past
    await create_cron_job(db, "sess1", "0 9 * * *", "due job", "admin", "2026-03-17T09:00:00")
    # Job due in the future
    await create_cron_job(db, "sess1", "0 9 * * *", "future job", "admin", "2026-12-31T09:00:00")

    due = await get_due_cron_jobs(db, "2026-03-17T12:00:00")
    assert len(due) == 1
    assert due[0]["prompt"] == "due job"


async def test_get_due_cron_jobs_skips_disabled(db):
    job_id = await create_cron_job(db, "sess1", "0 9 * * *", "disabled job", "admin", "2026-03-17T09:00:00")
    await update_cron_enabled(db, job_id, False)
    due = await get_due_cron_jobs(db, "2026-03-17T12:00:00")
    assert len(due) == 0


async def test_update_cron_last_run(db):
    job_id = await create_cron_job(db, "sess1", "0 9 * * *", "job A", "admin", "2026-03-17T09:00:00")
    await update_cron_last_run(db, job_id, "2026-03-17T09:00:00", "2026-03-18T09:00:00")
    jobs = await list_cron_jobs(db)
    assert jobs[0]["last_run"] == "2026-03-17T09:00:00"
    assert jobs[0]["next_run"] == "2026-03-18T09:00:00"


# --- Croniter integration ---


def test_croniter_next_run():
    """Verify croniter computes correct next_run from a cron expression."""
    from croniter import croniter
    base = datetime(2026, 3, 17, 12, 0, 0)
    cron = croniter("0 9 * * *", base)
    next_dt = cron.get_next(datetime)
    assert next_dt == datetime(2026, 3, 18, 9, 0, 0)


def test_croniter_every_5_minutes():
    from croniter import croniter
    base = datetime(2026, 3, 17, 12, 3, 0)
    cron = croniter("*/5 * * * *", base)
    next_dt = cron.get_next(datetime)
    assert next_dt == datetime(2026, 3, 17, 12, 5, 0)


def test_croniter_invalid_expression():
    from croniter import croniter
    assert not croniter.is_valid("invalid cron")
    assert croniter.is_valid("0 9 * * *")


# --- M679: _cron_scheduler loop ---


async def test_cron_scheduler_processes_due_jobs(db):
    """M679: _cron_scheduler fetches due jobs, saves message with source='cron',
    enqueues to worker, and updates next_run via croniter."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiso.store import create_cron_job, get_due_cron_jobs

    # Create a due job
    await create_cron_job(db, "sess1", "0 9 * * *", "check prices", "admin", "2026-03-17T09:00:00")

    call_count = 0

    async def _fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise asyncio.CancelledError  # stop loop after first iteration

    fake_queue = AsyncMock()

    with patch("kiso.main.asyncio.sleep", side_effect=_fake_sleep), \
         patch("kiso.main._ensure_worker", return_value=fake_queue) as mock_ensure, \
         patch("kiso.main.save_message", new_callable=AsyncMock, return_value=42) as mock_save_msg:
        from kiso.main import _cron_scheduler
        config = MagicMock()
        app = MagicMock()

        with pytest.raises(asyncio.CancelledError):
            await _cron_scheduler(db, config, app)

    # save_message called with source="cron"
    mock_save_msg.assert_called_once()
    call_kwargs = mock_save_msg.call_args
    assert call_kwargs[1]["source"] == "cron" or call_kwargs[0][5:] == (True, False, "cron") or "cron" in str(call_kwargs)
    # Positional: db, session, user, role, content, trusted, processed, source
    args = call_kwargs[0] if call_kwargs[0] else None
    kwargs = call_kwargs[1] if call_kwargs[1] else {}
    # Verify source="cron" either as positional or keyword
    if args and len(args) >= 8:
        assert args[7] == "cron"
    else:
        assert kwargs.get("source") == "cron"

    # Worker was ensured and queue.put was called
    mock_ensure.assert_called_once()
    fake_queue.put.assert_called_once()
    payload = fake_queue.put.call_args[0][0]
    assert payload["id"] == 42
    assert payload["content"] == "check prices"
    assert payload["username"] == "cron"


async def test_cron_scheduler_updates_next_run(db):
    """M679: After processing, next_run is updated via croniter."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiso.store import create_cron_job, list_cron_jobs

    job_id = await create_cron_job(
        db, "sess1", "0 9 * * *", "daily job", "admin", "2026-03-17T09:00:00",
    )

    call_count = 0

    async def _fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise asyncio.CancelledError

    fake_queue = AsyncMock()

    with patch("kiso.main.asyncio.sleep", side_effect=_fake_sleep), \
         patch("kiso.main._ensure_worker", return_value=fake_queue), \
         patch("kiso.main.save_message", new_callable=AsyncMock, return_value=1):
        from kiso.main import _cron_scheduler
        with pytest.raises(asyncio.CancelledError):
            await _cron_scheduler(db, MagicMock(), MagicMock())

    # Verify next_run was updated (should be 2026-03-18 since scheduler ran "now")
    jobs = await list_cron_jobs(db)
    assert len(jobs) == 1
    assert jobs[0]["last_run"] is not None
    assert jobs[0]["next_run"] != "2026-03-17T09:00:00"  # was updated


async def test_cron_scheduler_per_job_error_continues(db):
    """M679: Error processing one job doesn't crash the scheduler."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiso.store import create_cron_job

    await create_cron_job(db, "sess1", "0 9 * * *", "job A", "admin", "2026-03-17T09:00:00")

    call_count = 0

    async def _fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            raise asyncio.CancelledError

    # save_message raises on first call but scheduler should log and continue
    with patch("kiso.main.asyncio.sleep", side_effect=_fake_sleep), \
         patch("kiso.main._ensure_worker", side_effect=RuntimeError("boom")), \
         patch("kiso.main.save_message", new_callable=AsyncMock, return_value=1):
        from kiso.main import _cron_scheduler
        # Should not crash — logs error and retries next cycle
        with pytest.raises(asyncio.CancelledError):
            await _cron_scheduler(db, MagicMock(), MagicMock())
