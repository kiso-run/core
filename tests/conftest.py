"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from kiso.config import load_config
from kiso.main import app
from kiso.store import init_db


# ---------------------------------------------------------------------------
# Live LLM test gating
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--llm-live", action="store_true", default=False,
        help="Run live LLM integration tests (requires KISO_LLM_API_KEY)",
    )
    parser.addoption(
        "--live-network", action="store_true", default=False,
        help="Run tests that call external services (GitHub, etc.)",
    )


def pytest_collection_modifyitems(config, items):
    # --- llm_live gating ---
    if config.getoption("--llm-live"):
        if not os.environ.get("KISO_LLM_API_KEY"):
            skip = pytest.mark.skip(reason="KISO_LLM_API_KEY not set")
            for item in items:
                if "llm_live" in item.keywords:
                    item.add_marker(skip)
    else:
        skip = pytest.mark.skip(reason="Need --llm-live flag to run live LLM tests")
        for item in items:
            if "llm_live" in item.keywords:
                item.add_marker(skip)

    # --- live_network gating ---
    if not config.getoption("--live-network"):
        skip = pytest.mark.skip(reason="Need --live-network flag to run network tests")
        for item in items:
            if "live_network" in item.keywords:
                item.add_marker(skip)

VALID_CONFIG = """\
[tokens]
cli = "test-secret-token"
discord = "discord-bot-token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[users.testadmin]
role = "admin"

[users.testuser]
role = "user"
skills = "*"

[users.testuser.aliases]
discord = "TestUser#1234"
"""

AUTH_HEADER = {"Authorization": "Bearer test-secret-token"}
DISCORD_AUTH_HEADER = {"Authorization": "Bearer discord-bot-token"}


@pytest.fixture()
def test_config_path(tmp_path: Path) -> Path:
    """Write a valid config.toml to tmp_path and return its Path."""
    p = tmp_path / "config.toml"
    p.write_text(VALID_CONFIG)
    return p


@pytest.fixture()
def test_config(test_config_path: Path):
    """Load and return a Config from the test config file."""
    return load_config(test_config_path)


@pytest_asyncio.fixture()
async def db(tmp_path: Path):
    """Create a temporary database, yield connection, close on teardown."""
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest_asyncio.fixture()
async def client(tmp_path: Path, test_config_path: Path):
    """Async httpx client wired to the FastAPI app via ASGI transport.

    Directly sets app.state to bypass lifespan (httpx ASGITransport
    doesn't trigger lifespan events).
    """
    db_conn = await init_db(tmp_path / "client_test.db")
    app.state.config = load_config(test_config_path)
    app.state.db = db_conn
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db_conn.close()
