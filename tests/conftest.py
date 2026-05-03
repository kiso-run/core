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
        help="Run live LLM integration tests (requires OPENROUTER_API_KEY)",
    )
    parser.addoption(
        "--live-network", action="store_true", default=False,
        help="Run tests that call external services (GitHub, etc.)",
    )
    parser.addoption(
        "--functional", action="store_true", default=False,
        help="Run full pipeline functional tests (requires OPENROUTER_API_KEY)",
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
        if not os.environ.get("OPENROUTER_API_KEY"):
            skip = pytest.mark.skip(reason="OPENROUTER_API_KEY not set")
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
        if not os.environ.get("OPENROUTER_API_KEY"):
            skip = pytest.mark.skip(reason="OPENROUTER_API_KEY not set")
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
# Inline per-test duration in verbose output
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
# Zero-delay retry backoff in unit tests
# ---------------------------------------------------------------------------
# Transport retries (kiso.llm) and messenger retries (kiso.brain) use
# asyncio.sleep for backoff. In unit tests, these sleeps cause timeouts.
# This autouse fixture sets backoff to 0 without patching asyncio.sleep.

@pytest.fixture(scope="session", autouse=True)
def _isolated_kiso_dir(tmp_path_factory):
    """Patch ``kiso.brain.KISO_DIR`` to a tmp dir for the whole session.

    The dir is intentionally NOT pre-populated with bundled roles.
    The lazy self-heal in
    :func:`kiso.brain.prompts._load_system_prompt` copies each
    bundled role into the patched user dir on first access. This
    means:

    - Unit tests never accidentally write to the developer's real
      ``~/.kiso/`` because every load goes through the patched dir.
    - Tests that re-patch ``kiso.brain.KISO_DIR`` to their own
      ``tmp_path`` (function-scoped) keep working — the loader
      self-heals into whichever dir is currently patched.
    - Adding a new bundled role does NOT require any test fixture
      change; the loader picks it up automatically.
    """
    test_kiso_dir = tmp_path_factory.mktemp("kiso_test_home")
    p = patch("kiso.brain.KISO_DIR", test_kiso_dir)
    p.start()
    yield test_kiso_dir
    p.stop()


@pytest.fixture(autouse=True)
def _no_retry_backoff():
    """Set retry/delay constants to 0 for fast tests."""
    import kiso.llm
    import kiso.brain
    old_transport = kiso.llm._TRANSPORT_RETRY_BACKOFF
    old_rate = kiso.llm._RATE_INITIAL_BACKOFF
    old_messenger = kiso.brain._MESSENGER_RETRY_BACKOFF
    kiso.llm._TRANSPORT_RETRY_BACKOFF = 0.0
    kiso.llm._RATE_INITIAL_BACKOFF = 0.0
    kiso.brain._MESSENGER_RETRY_BACKOFF = 0.0
    kiso.llm._cb_reset()
    yield
    kiso.llm._cb_reset()
    kiso.llm._TRANSPORT_RETRY_BACKOFF = old_transport
    kiso.llm._RATE_INITIAL_BACKOFF = old_rate
    kiso.brain._MESSENGER_RETRY_BACKOFF = old_messenger


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
mcp = "*"
skills = "*"

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
# a single plan cycle is ~8+ LLM calls once classifier and
# briefers are included.
LLM_SINGLE_PLAN_TIMEOUT = 240
LLM_REPLAN_TIMEOUT = 300      # single request expected to hit reviewer/planner recovery
# Multi-wrapper or multi-plan request chains. Wrapper-heavy end-to-end
# flows (aider edit + exec + msg) need headroom for internal LLM
# round-trips on loaded hosts or slow provider days.
LLM_MULTI_PLAN_TIMEOUT = 900
LLM_INSTALL_TIMEOUT = 900     # wrapper install/download + LLM

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


# --- Shared test helper functions (importable, not fixtures) ---


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


# M1613/M1614: capability hints for `@pytest.mark.requires_mcp("<name>")`.
# When a marker name contains one of the keywords below, the auto-
# registration uses the matching capability-flavoured method name +
# description + realistic callback rather than the generic "default"
# stub. The planner reads the description (M1609 invariant) to decide
# whether the MCP covers the user's intent; downstream tests read the
# callback output to verify search-and-summarise / OCR / transcription
# pipelines end-to-end (M1614).
#
# Pattern matching is substring + case-insensitive. The first match in
# iteration order wins; later mocks fall through to the "default"
# fallback at the end. Keywords stay deliberately broad — they cover
# common categories (search, transcription, OCR, browser/page,
# network fetch) without enumerating specific server / vendor names.
# Callback content is generic technical text — no test-specific
# overfitting; F7's programming-language assertion happens to match
# because real search results often mention popular languages.


def _mock_search_callback(**kwargs):
    query = kwargs.get("query") or kwargs.get("q") or "<unspecified>"
    return (
        f"Mock search results for {query!r}:\n"
        "1. Python 3.12 release notes — typed dict updates and syntax changes\n"
        "2. JavaScript ES2024 features in V8 and Node.js\n"
        "3. TypeScript 5.5 type predicates and inference improvements\n"
        "4. Rust ownership patterns for systems programmers\n"
        "5. Go 1.22 routing rewrite and slog improvements\n"
        "6. Java records, sealed classes and pattern matching\n"
        "7. Kotlin Multiplatform mobile development guide\n"
        "8. Swift 6 strict concurrency overview\n"
        "9. C++ 23 ranges and modules cookbook\n"
        "10. Ruby 3.3 YJIT performance benchmarks\n"
    )


def _mock_transcribe_callback(**kwargs):
    return (
        "Welcome to the meeting recording. Today we discussed the project "
        "roadmap, focusing on three priorities: shipping the new release, "
        "improving test coverage, and onboarding two new engineers. The next "
        "sync is scheduled for Friday at 10am. Thank you for attending."
    )


def _mock_extract_text_callback(**kwargs):
    return (
        "Invoice #INV-2025-0142\n"
        "Date: 2025-04-15\n"
        "Bill to: Acme Corporation\n"
        "Total: 1,250.00 EUR\n"
        "Payment terms: net 30 days\n"
    )


def _mock_navigate_callback(**kwargs):
    url = kwargs.get("url") or "<unspecified>"
    return (
        f"Page content from {url}:\n"
        "Welcome to our documentation. This page covers installation, "
        "configuration, and a getting-started tutorial. See also the API "
        "reference and the troubleshooting guide for common issues."
    )


def _mock_fetch_callback(**kwargs):
    url = kwargs.get("url") or "<unspecified>"
    return f'{{"status": 200, "url": "{url}", "body": "Mock response body for testing"}}'


def _mock_translate_callback(**kwargs):
    text = kwargs.get("text") or "<unspecified>"
    return f"[mock translation of {text!r}]"


def _make_default_callback(name: str):
    """Default fallback callback factory; returns the legacy
    ``[mock response from <name>:default]`` string per the M1581
    contract preserved by ``test_default_stub_returns_canonical_string``.
    """
    def _cb(**kwargs):
        return f"[mock response from {name}:default]"
    return _cb


_CAPABILITY_HINTS: list[tuple[tuple[str, ...], str, str, "Callable"]] = [
    (("search", "web-search"), "search",
     "Search the web for a query and return ranked results.",
     _mock_search_callback),
    (("transcrib", "speech", "whisper"), "transcribe",
     "Transcribe an audio file to text.",
     _mock_transcribe_callback),
    (("ocr", "text-extract", "image-text"), "extract_text",
     "Extract text from an image via OCR.",
     _mock_extract_text_callback),
    (("browser", "playwright", "page", "navigat"), "navigate",
     "Navigate to a URL and return page content.",
     _mock_navigate_callback),
    (("fetch", "http"), "fetch",
     "Fetch a URL and return the response body.",
     _mock_fetch_callback),
    (("translat",), "translate",
     "Translate text from one language to another.",
     _mock_translate_callback),
]


def _capability_method_for_mcp_name(
    name: str,
):
    """Return ``(method_name, description, callback)`` for a given MCP name.

    Looks the lower-cased ``name`` up against ``_CAPABILITY_HINTS``;
    returns the first match. Falls back to a generic ``default`` stub
    when no keyword matches — older tests that register with arbitrary
    names still work unchanged. The default fallback's callback is
    name-bound so the legacy ``[mock response from <name>:default]``
    string contract holds.
    """
    lname = name.lower()
    for keywords, method_name, description, callback in _CAPABILITY_HINTS:
        if any(kw in lname for kw in keywords):
            return method_name, description, callback
    return "default", "mock method default", _make_default_callback(name)


@pytest.fixture()
def mock_mcp_catalog(request):
    """M1580: per-test handle for the in-process Mock MCP framework.

    Tests register fake MCPs with arbitrary names + method callbacks
    via `mock_mcp_catalog.register(...)` and then build a wired
    `MCPManager` via `.build_manager()`. Catalog visibility flows
    through the same code path the briefer uses in production. See
    `tests/_mcp_mock.py` for the full API.

    M1581: when a test is decorated with
    `@pytest.mark.requires_mcp("name")` (or
    `@pytest.mark.requires_mcp(["a", "b"])`) the catalog is
    auto-populated with a stub for each named MCP.

    M1613: the auto-registered stub now uses a capability-flavoured
    method name + description (e.g. ``search-mcp`` → method
    ``search`` with description "Search the web for a query and
    return ranked results.") so the planner / briefer can match the
    declared capability against the user's intent (M1609 invariant).
    Names that don't match any capability keyword fall through to the
    generic ``default`` method, preserving back-compat. Tests that
    need richer or alternate methods can call ``register`` again —
    later registrations overwrite earlier ones.
    """
    from tests._mcp_mock import MockMCPCatalog
    catalog = MockMCPCatalog()
    marker = request.node.get_closest_marker("requires_mcp")
    if marker is not None and marker.args:
        names = marker.args[0]
        if isinstance(names, str):
            names = [names]
        for name in names:
            method_name, description, callback = (
                _capability_method_for_mcp_name(name)
            )
            catalog.register(
                name,
                {method_name: callback},
                descriptions={method_name: description},
            )
    return catalog
