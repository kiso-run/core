"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from kiso.config import load_config
from kiso.main import app, _init_app_state, _rate_limiter
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
    parser.addoption(
        "--functional", action="store_true", default=False,
        help="Run full pipeline functional tests (requires KISO_LLM_API_KEY)",
    )
    parser.addoption(
        "--destructive", action="store_true", default=False,
        help="Run destructive functional tests with real side effects",
    )
    parser.addoption(
        "--integration", action="store_true", default=False,
        help="Run connector protocol integration tests",
    )
    parser.addoption(
        "--interactive", action="store_true", default=False,
        help="Run interactive tests requiring a human at the terminal",
    )
    parser.addoption(
        "--extended", action="store_true", default=False,
        help="Run extended (long-running) functional tests",
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

    # --- functional gating ---
    # Use iter_markers() instead of keyword lookup because pytest adds
    # the directory name "functional" as a keyword to every test in
    # tests/functional/, which would match falsely.
    def _has_marker(item, name):
        return next(item.iter_markers(name), None) is not None

    if config.getoption("--functional"):
        if not os.environ.get("KISO_LLM_API_KEY"):
            skip = pytest.mark.skip(reason="KISO_LLM_API_KEY not set")
            for item in items:
                if _has_marker(item, "functional"):
                    item.add_marker(skip)
    else:
        skip = pytest.mark.skip(reason="Need --functional flag to run functional tests")
        for item in items:
            if _has_marker(item, "functional"):
                item.add_marker(skip)

    # --- destructive gating ---
    if not config.getoption("--destructive"):
        skip = pytest.mark.skip(reason="Need --destructive flag to run destructive tests")
        for item in items:
            if _has_marker(item, "destructive"):
                item.add_marker(skip)

    # --- integration gating ---
    if not config.getoption("--integration"):
        skip = pytest.mark.skip(reason="Need --integration flag to run integration tests")
        for item in items:
            if _has_marker(item, "integration"):
                item.add_marker(skip)

    # --- interactive gating ---
    if not config.getoption("--interactive"):
        skip = pytest.mark.skip(reason="Need --interactive flag to run interactive tests")
        for item in items:
            if _has_marker(item, "interactive"):
                item.add_marker(skip)

    # --- extended gating ---
    # Extended tests are also functional — they need --functional AND --extended.
    # When --functional is set but --extended is not, skip extended tests.
    if not config.getoption("--extended"):
        skip = pytest.mark.skip(reason="Need --extended flag to run extended tests")
        for item in items:
            if _has_marker(item, "extended"):
                item.add_marker(skip)

# ---------------------------------------------------------------------------
# M479/M480: Zero-delay retry backoff in unit tests
# ---------------------------------------------------------------------------
# Transport retries (kiso.llm) and messenger retries (kiso.brain) use
# asyncio.sleep for backoff. In unit tests, these sleeps cause timeouts.
# This autouse fixture sets backoff to 0 without patching asyncio.sleep.

@pytest.fixture(autouse=True)
def _no_retry_backoff():
    """Set retry/delay constants to 0 for fast tests."""
    import kiso.llm
    import kiso.brain
    import kiso.worker.loop
    old_transport = kiso.llm._TRANSPORT_RETRY_BACKOFF
    old_messenger = kiso.brain._MESSENGER_RETRY_BACKOFF
    old_rescan = kiso.worker.loop._POST_INSTALL_RESCAN_DELAY
    kiso.llm._TRANSPORT_RETRY_BACKOFF = 0.0
    kiso.brain._MESSENGER_RETRY_BACKOFF = 0.0
    kiso.worker.loop._POST_INSTALL_RESCAN_DELAY = 0.0
    kiso.llm._cb_reset()
    yield
    kiso.llm._cb_reset()
    kiso.llm._TRANSPORT_RETRY_BACKOFF = old_transport
    kiso.brain._MESSENGER_RETRY_BACKOFF = old_messenger
    kiso.worker.loop._POST_INSTALL_RESCAN_DELAY = old_rescan


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
tools = "*"

[users.testuser.aliases]
discord = "TestUser#1234"

[models]
briefer     = "test-briefer"
classifier  = "test-classifier"
planner     = "test-planner"
reviewer    = "test-reviewer"
curator     = "test-curator"
worker      = "test-worker"
summarizer  = "test-summarizer"
paraphraser = "test-paraphraser"
messenger   = "test-messenger"
searcher    = "test-searcher"

[settings]
context_messages          = 7
summarize_threshold       = 30
bot_name                  = "Kiso"
knowledge_max_facts       = 50
fact_decay_days           = 7
fact_decay_rate           = 0.1
fact_archive_threshold    = 0.3
fact_consolidation_min_ratio = 0.3
max_replan_depth          = 3
max_validation_retries    = 3
max_plan_tasks            = 20
llm_timeout              = 5
max_output_size           = 1048576
max_worker_retries        = 1
max_llm_calls_per_message = 200
max_message_size          = 65536
max_queue_size            = 50
host                      = "0.0.0.0"
port                      = 8333
worker_idle_timeout       = 1
fast_path_enabled         = true
briefer_enabled           = false
webhook_allow_list        = []
webhook_require_https     = true
webhook_secret            = ""
webhook_max_payload       = 1048576
"""

AUTH_HEADER = {"Authorization": "Bearer test-secret-token"}
DISCORD_AUTH_HEADER = {"Authorization": "Bearer discord-bot-token"}

# Centralized test timeouts (seconds). Override per-test if needed.
LLM_TEST_TIMEOUT = 120       # single plan cycle (~6 LLM calls)
LLM_REPLAN_TIMEOUT = 300     # replan cycle (~12+ LLM calls)
LLM_INSTALL_TIMEOUT = 900    # tool install (network download + LLM)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear rate-limiter state between tests to prevent interference."""
    _rate_limiter.reset()
    yield
    _rate_limiter.reset()


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
async def db_with_session(tmp_path: Path):
    """Create a temporary database with a pre-created 'sess1' session."""
    from kiso.store import create_session
    conn = await init_db(tmp_path / "test.db")
    await create_session(conn, "sess1")
    yield conn
    await conn.close()


# --- M706: Shared test helper functions (importable, not fixtures) ---


def full_settings(**overrides) -> dict:
    """Return a complete settings dict with briefer disabled by default."""
    from kiso.config import SETTINGS_DEFAULTS
    return {**SETTINGS_DEFAULTS, "briefer_enabled": False, **overrides}


def full_models(**overrides) -> dict:
    """Return a complete models dict with optional overrides."""
    from kiso.config import MODEL_DEFAULTS
    return {**MODEL_DEFAULTS, **overrides}


@pytest_asyncio.fixture()
async def client(tmp_path: Path, test_config_path: Path):
    """Async httpx client wired to the FastAPI app via ASGI transport.

    Directly sets app.state to bypass lifespan (httpx ASGITransport
    doesn't trigger lifespan events).
    """
    db_conn = await init_db(tmp_path / "client_test.db")
    _init_app_state(app, load_config(test_config_path), db_conn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db_conn.close()
