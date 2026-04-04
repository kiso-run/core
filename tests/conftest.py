"""Shared test fixtures."""

from __future__ import annotations

import os
from contextlib import contextmanager
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
# M927: Inline per-test duration in verbose output
# ---------------------------------------------------------------------------

def pytest_report_teststatus(report, config):
    if report.when != "call":
        return
    secs = report.duration
    if secs >= 60:
        m, s = divmod(int(secs), 60)
        dur = f"{m}m {s}s"
    elif secs >= 0.05:
        dur = f"{secs:.1f}s"
    else:
        dur = "0.0s"
    # Return plain text — no ANSI codes.  Embedded ANSI causes pytest's
    # TerminalWriter to miscalculate line width, breaking progress padding.
    # pytest applies its own coloring to the returned word.
    if report.passed:
        return "passed", ".", f"PASSED ({dur})"
    if report.failed:
        return "failed", "F", f"FAILED ({dur})"


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
    old_rate = kiso.llm._RATE_INITIAL_BACKOFF
    old_messenger = kiso.brain._MESSENGER_RETRY_BACKOFF
    old_rescan = kiso.worker.loop._POST_INSTALL_RESCAN_DELAY
    kiso.llm._TRANSPORT_RETRY_BACKOFF = 0.0
    kiso.llm._RATE_INITIAL_BACKOFF = 0.0
    kiso.brain._MESSENGER_RETRY_BACKOFF = 0.0
    kiso.worker.loop._POST_INSTALL_RESCAN_DELAY = 0.0
    kiso.llm._cb_reset()
    yield
    kiso.llm._cb_reset()
    kiso.llm._TRANSPORT_RETRY_BACKOFF = old_transport
    kiso.llm._RATE_INITIAL_BACKOFF = old_rate
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
consolidator = "test-consolidator"

[settings]
context_messages          = 7
summarize_threshold       = 30
bot_name                  = "Kiso"
knowledge_max_facts       = 50
fact_decay_days           = 7
fact_decay_rate           = 0.1
fact_archive_threshold    = 0.3
fact_consolidation_min_ratio = 0.3
consolidation_enabled             = false
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

# Centralized workflow-class timeouts (seconds).
# Keep these coarse and stable: per-test hardcoded numbers made it hard to tell
# whether a timeout change reflected a real workflow-class shift or just local
# drift. Pick the smallest class that matches the behavior under test so
# deterministic infinite-loop regressions still fail promptly.
LLM_ROLE_ONLY_TIMEOUT = 180   # direct role calls: planner/reviewer/worker/etc.
# M1062/M1057/M1081: a single plan cycle is ~8+ LLM calls once classifier and
# briefers are included.
LLM_SINGLE_PLAN_TIMEOUT = 240
LLM_REPLAN_TIMEOUT = 300      # single request expected to hit reviewer/planner recovery
LLM_MULTI_PLAN_TIMEOUT = 600  # multi-tool or multi-plan request chains
LLM_INSTALL_TIMEOUT = 900     # tool install/download + LLM

# Backward-compatible alias for older tests that haven't been migrated to the
# more specific workflow classes yet.
LLM_TEST_TIMEOUT = LLM_SINGLE_PLAN_TIMEOUT


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


@contextmanager
def patch_kiso_dir(tmp_path):
    """Patch KISO_DIR in worker submodules and disable disk limit check."""
    with patch("kiso.worker.utils.KISO_DIR", tmp_path), \
         patch("kiso.worker.loop.KISO_DIR", tmp_path), \
         patch("kiso.worker.loop._check_disk_limit", return_value=None):
        yield


def make_config(**overrides) -> "Config":
    """Build a :class:`Config` for tests with sensible defaults.

    ``settings`` may be passed as a nested dict — its entries are *merged*
    into the base settings rather than replacing them.  All other keyword
    arguments are forwarded straight to :class:`Config`.
    """
    from kiso.config import Config, Provider

    base_settings = full_settings(worker_idle_timeout=0.05, llm_timeout=5)
    if "settings" in overrides:
        base_settings.update(overrides.pop("settings"))
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"local": Provider(base_url="http://localhost:11434/v1")},
        users={},
        models=full_models(planner="gpt-4", worker="gpt-3.5", reviewer="gpt-4"),
        settings=base_settings,
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


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
