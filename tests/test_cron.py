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
