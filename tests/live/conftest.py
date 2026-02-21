"""Fixtures for live LLM integration tests."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from kiso.config import (
    Config,
    ConfigError,
    MODEL_DEFAULTS,
    Provider,
    SETTINGS_DEFAULTS,
    User,
)
from kiso.store import create_session, init_db, save_message


@pytest.fixture(scope="session")
def live_config() -> Config:
    """Config wired to the real OpenRouter provider (same models as production)."""
    settings = {
        **SETTINGS_DEFAULTS,
        "exec_timeout": 60,
        "max_validation_retries": 3,
    }
    return Config(
        tokens={"cli": "test-live-token"},
        providers={
            "openrouter": Provider(
                base_url="https://openrouter.ai/api/v1",
            ),
        },
        users={"testadmin": User(role="admin")},
        models={**MODEL_DEFAULTS},
        settings=settings,
        raw={},
    )


@pytest_asyncio.fixture()
async def live_db(tmp_path: Path):
    """Fresh SQLite database for each test."""
    conn = await init_db(tmp_path / "live_test.db")
    yield conn
    await conn.close()


@pytest.fixture()
def live_session() -> str:
    """Unique session ID for each test."""
    return f"live-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture()
async def seeded_db(live_db, live_session):
    """live_db with a session row already created."""
    await create_session(live_db, live_session)
    return live_db


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_noop_infra():
    """Patch away filesystem/security/webhook, let LLM calls flow through.

    Returns a context manager; use as ``with mock_noop_infra(): ...``.
    """
    return patch.multiple(
        "kiso.worker",
        reload_config=MagicMock(side_effect=ConfigError("test")),
        _ensure_sandbox_user=MagicMock(return_value=None),
        revalidate_permissions=MagicMock(
            return_value=MagicMock(allowed=True, role="admin"),
        ),
        collect_deploy_secrets=MagicMock(return_value={}),
        deliver_webhook=AsyncMock(return_value=(True, 200, 1)),
    )


@pytest_asyncio.fixture()
async def live_msg(seeded_db, live_session):
    """Save a message to DB and return the msg dict ready for _process_message.

    Usage::

        msg = await live_msg("What is 2+2?")
    """

    async def _make(content: str, *, user_role: str = "admin") -> dict:
        msg_id = await save_message(
            seeded_db, live_session, "testadmin", "user", content,
        )
        return {
            "id": msg_id,
            "content": content,
            "user_role": user_role,
            "user_skills": "*",
            "username": "testadmin",
        }

    return _make
