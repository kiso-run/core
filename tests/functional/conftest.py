"""Fixtures and helpers for functional acceptance tests.

Functional tests exercise the full kiso pipeline (classifier → planner →
worker → skills → messenger) with real LLM, real network, and real skill
execution.  They are gated by ``--functional`` and optionally
``--destructive`` pytest flags.
"""

from __future__ import annotations

import logging

# Enable INFO logging for functional tests — shows classifier results,
# planner decisions, validation errors, and LLM call details.
logging.basicConfig(level=logging.INFO)

import asyncio
import fnmatch
import getpass
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

log = logging.getLogger(__name__)

import pytest
import pytest_asyncio

from kiso.config import (
    Config,
    MODEL_DEFAULTS,
    Provider,
    SETTINGS_DEFAULTS,
    User,
)
from kiso.wrappers import discover_wrappers, invalidate_wrappers_cache
from kiso.main import _collect_boot_facts, _init_ssh_keys
from kiso.store import create_session, init_db, save_message
from kiso.worker.loop import _process_message


# NOTE: individual test files that need the ``functional`` marker should set
# ``pytestmark = pytest.mark.functional`` themselves.  The conftest does NOT
# apply it globally so that helper-only tests (test_helpers.py) can run
# without ``--functional``.


# ---------------------------------------------------------------------------
# Language assertion helpers
# ---------------------------------------------------------------------------

# Common Italian function words (articles, prepositions, conjunctions, verbs).
_IT_WORDS = frozenset(
    "il la le lo gli di del della dei delle dello che è e per un una uno "
    "sono ha con non più anche come questo nella nel nei sul sulla al alla "
    "da dei si in".split()
)

# Common English function words.
_EN_WORDS = frozenset(
    "the is are was of and to in for with that this have has from but not "
    "can will which an it be been would should could their there been".split()
)

# Common Spanish function words.
_ES_WORDS = frozenset(
    "el la los las es de del que por para un una con en se su al más pero "
    "como este esta son hay muy sin entre sobre todo cada".split()
)

# Common Russian function words.
_RU_WORDS = frozenset(
    "и в не на я что он с это как но из к за то по она мы они был бы"
    " все так же от его до бы ее мне ему нет да".split()
)


def tool_installed(name: str) -> bool:
    """Return True when a named tool is currently installed."""
    invalidate_wrappers_cache()
    return any(t["name"] == name for t in discover_wrappers())

_LANG_WORDS = {"it": _IT_WORDS, "en": _EN_WORDS, "es": _ES_WORDS, "ru": _RU_WORDS}
_LANG_NAMES = {"it": "Italian", "en": "English", "es": "Spanish", "ru": "Russian",
               "zh": "Chinese"}

_WORD_RE = re.compile(r"[a-zàèéìòùáéíóúñüа-яё]+", re.IGNORECASE)

# Strip fenced code blocks so code keywords don't skew language heuristics.
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)


_LIST_ITEM_RE = re.compile(r"^\s*[-*]\s+.*$", re.MULTILINE)
_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+\.\s+.*$", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^>.*$", re.MULTILINE)

# Multilingual regex for text stats assertions (chars/lines count).
# Matches: "chars: 1432", "**Character Count:** 1432", "caratteri: 92", etc.
CHARS_COUNT_RE = re.compile(
    r"(?:char(?:acter)?s?|caratteri)\s*(?:count)?\s*[:*]*\s*\d+", re.IGNORECASE,
)
LINES_COUNT_RE = re.compile(
    r"(?:lines?|righe?)\s*(?:count)?\s*[:*]*\s*\d+", re.IGNORECASE,
)


def _strip_code_blocks(text: str) -> str:
    """Remove Markdown fenced code blocks from *text*."""
    return _CODE_BLOCK_RE.sub("", text)


def _strip_quoted_content(text: str) -> str:
    """Remove code blocks, list items, and blockquotes from *text*.

    Messenger failure language appears in prose paragraphs. Scraped/cited
    content appears as markdown list items, numbered lists, or blockquotes.
    Stripping these prevents false positives from external content that
    happens to contain words like "errore" (e.g. sports headlines).
    """
    text = _CODE_BLOCK_RE.sub("", text)
    text = _LIST_ITEM_RE.sub("", text)
    text = _NUMBERED_ITEM_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    return text


def assert_language(text: str, lang: str) -> None:
    """Assert that *text* is predominantly in *lang*.

    Supports: it, en, es, ru (function-word scoring) and zh (CJK character detection).
    Code blocks, blockquotes, and list items are stripped first.
    """
    cleaned = _strip_quoted_content(text)
    name = _LANG_NAMES.get(lang, lang)

    # Chinese: detect by CJK Unified Ideographs character presence
    if lang == "zh":
        cjk_count = sum(1 for c in cleaned if "\u4e00" <= c <= "\u9fff")
        assert cjk_count > 5, (
            f"Text does not appear to be {name} "
            f"(CJK chars: {cjk_count}). First 200 chars: {text[:200]}"
        )
        return

    # Function-word scoring for Latin/Cyrillic scripts
    scores: dict[str, int] = {k: 0 for k in _LANG_WORDS}
    for w in _WORD_RE.findall(cleaned):
        wl = w.lower()
        for k, words in _LANG_WORDS.items():
            scores[k] += wl in words
    target = scores[lang]
    assert target > 0, (
        f"Text does not appear to be {name} "
        f"(no {name} function words found). First 200 chars: {text[:200]}"
    )
    best_other = max(v for k, v in scores.items() if k != lang)
    assert target >= best_other, (
        f"Text does not appear to be {name} "
        f"(scores: {', '.join(f'{_LANG_NAMES[k]}={v}' for k, v in scores.items())}). "
        f"First 200 chars: {text[:200]}"
    )


def assert_italian(text: str) -> None:
    """Assert that *text* is predominantly Italian."""
    assert_language(text, "it")


def assert_english(text: str) -> None:
    """Assert that *text* is predominantly English."""
    assert_language(text, "en")


def assert_russian(text: str) -> None:
    """Assert that *text* is predominantly Russian."""
    assert_language(text, "ru")


def assert_chinese(text: str) -> None:
    """Assert that *text* is predominantly Chinese."""
    assert_language(text, "zh")


def normalize_for_assertion(text: str, *, latin: bool = True) -> str:
    """Normalize LLM output for assertion matching.

    When ``latin=True`` (default): strip accents (NFKD), lowercase.
    When ``latin=False``: lowercase only (accent stripping is destructive
    for Cyrillic/CJK scripts).
    """
    import unicodedata
    text = text.lower()
    if latin:
        text = "".join(
            c for c in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(c)
        )
    return text


def assert_spanish(text: str) -> None:
    """Assert that *text* is predominantly Spanish."""
    assert_language(text, "es")


# ---------------------------------------------------------------------------
# URL reachability helper
# ---------------------------------------------------------------------------


async def assert_url_reachable(
    url: str,
    *,
    client: "httpx.AsyncClient | None" = None,
    expected_type: str | None = None,
    min_size: int = 1,
    timeout: float = 30,
) -> None:
    """Assert that *url* is reachable (HTTP 200) and optionally check Content-Type.

    Parameters
    ----------
    url:
        Full URL to GET.
    client:
        Optional httpx.AsyncClient to use (e.g. ASGI-backed for functional tests).
        When provided, the URL is fetched through this client instead of making
        a real HTTP request.
    expected_type:
        If provided, assert ``Content-Type`` starts with this string
        (e.g. ``"image"``).
    min_size:
        Minimum response body size in bytes.
    timeout:
        HTTP request timeout in seconds.
    """
    import httpx

    if client is not None:
        resp = await client.get(url, follow_redirects=True)
    else:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as c:
            resp = await c.get(url)
    assert resp.status_code == 200, (
        f"URL {url} returned status {resp.status_code}"
    )
    assert len(resp.content) >= min_size, (
        f"URL {url} returned only {len(resp.content)} bytes (min {min_size})"
    )
    if expected_type is not None:
        ct = resp.headers.get("content-type", "")
        assert ct.startswith(expected_type), (
            f"URL {url} Content-Type is {ct!r}, expected {expected_type}*"
        )


# ---------------------------------------------------------------------------
# Failure-language check
# ---------------------------------------------------------------------------

_FAILURE_PATTERNS = re.compile(
    r"non riesco|impossibile|errore|error:|failed to|cannot|couldn'?t",
    re.IGNORECASE,
)

_PUB_URL_RE = re.compile(r"https?://\S+/pub/\S+")


def assert_no_failure_language(text: str) -> None:
    """Assert that *text* does not contain obvious failure indicators.

    Code blocks, markdown list items, numbered lists, and blockquotes are
    stripped first so that technical code and scraped/cited content don't
    trigger false positives.
    """
    cleaned = _strip_quoted_content(text)
    match = _FAILURE_PATTERNS.search(cleaned)
    assert match is None, (
        f"Failure language detected: {match.group()!r} in: {text[:300]}"
    )


# ---------------------------------------------------------------------------
# FunctionalResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class FunctionalResult:
    """Structured result of a functional test run."""

    success: bool
    plans: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    msg_output: str = ""
    pub_files: list[dict] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def last_plan_msg_output(self) -> str:
        """Msg output from the last plan only (excludes prior turns)."""
        if not self.plans:
            return ""
        last_plan_id = self.plans[-1]["id"]
        return "\n".join(
            t.get("output", "") or ""
            for t in self.tasks
            if t.get("type") == "msg" and t.get("status") == "done"
            and t.get("plan_id") == last_plan_id
        )

    def has_published_file(self, glob_pattern: str) -> bool:
        """Check if any published file matches *glob_pattern*."""
        return any(
            fnmatch.fnmatch(f.get("filename", ""), glob_pattern)
            for f in self.pub_files
        )

    def task_types(self) -> list[str]:
        """Return list of task types in execution order."""
        return [t.get("type", "?") for t in self.tasks]

    def tool_tasks(self) -> list[dict]:
        """Return only tool-type tasks."""
        return [t for t in self.tasks if t.get("type") == "wrapper"]

    @staticmethod
    def task_wrapper_name(task: dict) -> str:
        """Return the tool name for a task (handles DB column `skill` vs `tool`)."""
        return task.get("skill") or task.get("tool") or ""


# ---------------------------------------------------------------------------
# Config fixture (session-scoped — same LLM config for all functional tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def func_config() -> Config:
    """Real Config wired to OpenRouter with generous timeouts."""
    settings = {
        **SETTINGS_DEFAULTS,
        "llm_timeout": 180,
        "max_validation_retries": 3,
        "max_replan_depth": 5,
        "max_llm_calls_per_message": 300,
    }
    return Config(
        tokens={"cli": "func-test-token"},
        providers={
            "openrouter": Provider(
                base_url="https://openrouter.ai/api/v1",
            ),
        },
        users={
            "testadmin": User(role="admin"),
            getpass.getuser(): User(role="admin"),
        },
        models={**MODEL_DEFAULTS},
        settings=settings,
        raw={},
    )


# ---------------------------------------------------------------------------
# Per-test DB and session
# ---------------------------------------------------------------------------


# Modules that import KISO_DIR at module level and need patching.
_KISO_DIR_MODULES = [
    "kiso.config",
    "kiso.brain",
    "kiso.wrappers",
    "kiso.main",
    "kiso.pub",
    "kiso.log",
    "kiso.audit",
    "kiso.sysenv",
    "kiso.connectors",
    "kiso.recipe_loader",
    "kiso.wrapper_repair",
    "kiso.worker.loop",
    "kiso.worker.utils",
]


def _write_test_config(kiso_dir: Path, cfg: Config) -> None:
    """Write a minimal config.toml so subprocess CLI commands can load it."""
    lines = ["[tokens]"]
    for k, v in cfg.tokens.items():
        lines.append(f'{k} = "{v}"')

    for name, prov in cfg.providers.items():
        lines.append(f"\n[providers.{name}]")
        lines.append(f'base_url = "{prov.base_url}"')

    for name, user in cfg.users.items():
        lines.append(f"\n[users.{name}]")
        lines.append(f'role = "{user.role}"')

    lines.append("\n[models]")
    for role, model in cfg.models.items():
        lines.append(f'{role} = "{model}"')

    lines.append("\n[settings]")
    for k, v in cfg.settings.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        elif isinstance(v, list):
            items = ", ".join(f'"{i}"' if isinstance(i, str) else str(i) for i in v)
            lines.append(f"{k} = [{items}]")
        else:
            lines.append(f"{k} = {v}")

    (kiso_dir / "config.toml").write_text("\n".join(lines) + "\n")


@pytest.fixture(scope="session", autouse=True)
def _func_kiso_dir(func_config, tmp_path_factory):
    """Isolate functional tests from the host ~/.kiso directory.

    Creates a temp KISO_DIR with clean ``wrappers/`` and ``sys/ssh/`` dirs,
    patches every module that imports KISO_DIR, stubs ``reload_config``
    so mid-execution config reloads return func_config, writes a config.toml
    so subprocess CLI commands can load it, and sets KISO_HOME so subprocess
    processes resolve KISO_DIR to the isolated directory.
    SSH keys are generated inside the temp dir.
    """
    kiso_dir = tmp_path_factory.mktemp("kiso_home")
    (kiso_dir / "wrappers").mkdir()
    (kiso_dir / "sys" / "ssh").mkdir(parents=True)

    # write config.toml for subprocess CLI commands
    _write_test_config(kiso_dir, func_config)

    # set KISO_HOME so subprocess processes use the isolated dir
    old_kiso_home = os.environ.get("KISO_HOME")
    os.environ["KISO_HOME"] = str(kiso_dir)

    patches = [patch(f"{mod}.KISO_DIR", kiso_dir) for mod in _KISO_DIR_MODULES]
    patches.append(
        patch("kiso.worker.loop.reload_config", return_value=func_config),
    )

    for p in patches:
        p.start()

    # Generate SSH keys in the isolated dir (mirrors FastAPI lifespan).
    _init_ssh_keys()

    yield kiso_dir

    for p in patches:
        p.stop()

    # Restore KISO_HOME
    if old_kiso_home is None:
        os.environ.pop("KISO_HOME", None)
    else:
        os.environ["KISO_HOME"] = old_kiso_home


@pytest_asyncio.fixture()
async def func_db(tmp_path: Path):
    """Fresh SQLite database for each functional test."""
    conn = await init_db(tmp_path / "func_test.db")
    yield conn
    await conn.close()


@pytest.fixture()
def func_session() -> str:
    """Unique session ID per test."""
    return f"func-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture()
async def func_app_client(func_config, func_db):
    """ASGI-backed httpx client wired to the FastAPI app.

    Uses the functional test's config and DB so that pub file serving
    works correctly (same sessions, same pub tokens).
    """
    import httpx
    from httpx import ASGITransport
    from kiso.main import app, _init_app_state

    _init_app_state(app, func_config, func_db)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# run_message — the core functional test helper
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def run_message(func_config, func_db, func_session):
    """Factory fixture: send a message through the full pipeline.

    Usage::

        result = await run_message("vai su guidance.studio e dimmi cosa fa")
        assert result.success
        assert_italian(result.msg_output)
    """
    # Inject boot facts (SSH key, hostname, version) — mirrors FastAPI lifespan
    await _collect_boot_facts(func_db)

    async def _run(
        content: str,
        *,
        timeout: float = 300,
        base_url: str = "http://test",
    ) -> FunctionalResult:
        # Ensure session exists (may already exist in multi-message tests)
        try:
            await create_session(func_db, func_session)
        except Exception:  # noqa: BLE001 — IntegrityError on duplicate
            pass

        msg_id = await save_message(
            func_db, func_session, "testadmin", "user", content,
        )
        msg = {
            "id": msg_id,
            "content": content,
            "user_role": "admin",
            "user_wrappers": "*",
            "username": "testadmin",
            "base_url": base_url,
        }

        cancel_event = asyncio.Event()
        t0 = time.monotonic()

        bg_task = await asyncio.wait_for(
            _process_message(
                func_db,
                func_config,
                func_session,
                msg,
                cancel_event,
                llm_timeout=func_config.settings["llm_timeout"],
                max_replan_depth=func_config.settings["max_replan_depth"],
                messenger_timeout=func_config.settings["llm_timeout"],
            ),
            timeout=timeout,
        )
        # Wait for background knowledge task (curator + summarizer).
        # increased from 30s to 90s — curator with transport retries
        # can easily exceed 30s.  Log instead of silently swallowing.
        if bg_task is not None and not bg_task.done():
            try:
                await asyncio.wait_for(bg_task, timeout=90)
            except asyncio.TimeoutError:
                log.warning("Background knowledge task timed out after 90s")
            except Exception as e:
                log.warning("Background knowledge task failed: %s", e)

        elapsed = time.monotonic() - t0

        # Collect results from DB
        cur = await func_db.execute(
            "SELECT * FROM plans WHERE session = ? ORDER BY id",
            (func_session,),
        )
        plans = [dict(r) for r in await cur.fetchall()]

        all_tasks: list[dict] = []
        for p in plans:
            cur2 = await func_db.execute(
                "SELECT * FROM tasks WHERE plan_id = ? ORDER BY id",
                (p["id"],),
            )
            all_tasks.extend(dict(r) for r in await cur2.fetchall())

        # Determine success: last plan status is "done"
        success = bool(plans and plans[-1].get("status") == "done")

        # Concatenate msg task outputs
        msg_output = "\n".join(
            t.get("output", "") or ""
            for t in all_tasks
            if t.get("type") == "msg" and t.get("status") == "done"
        )

        # Collect pub files (from task outputs that mention /pub/)
        pub_files: list[dict] = []
        for t in all_tasks:
            output = t.get("output") or ""
            for url in _PUB_URL_RE.findall(output):
                filename = url.rsplit("/", 1)[-1] if "/" in url else url
                pub_files.append({"filename": filename, "url": url})

        return FunctionalResult(
            success=success,
            plans=plans,
            tasks=all_tasks,
            msg_output=msg_output,
            pub_files=pub_files,
            elapsed=elapsed,
        )

    return _run


# ---------------------------------------------------------------------------
# Tool install helpers (shared across functional test files)
# ---------------------------------------------------------------------------


_PRESET_TOOLS = ["browser", "ocr", "aider"]


@pytest.fixture(scope="session")
def preset_tools_installed():
    """Install browser, ocr, and aider before any test that needs them.

    Session-scoped — runs once per test session.
    """
    import subprocess
    from kiso.wrappers import discover_wrappers, invalidate_wrappers_cache

    invalidate_wrappers_cache()
    installed = {t["name"] for t in discover_wrappers()}

    for name in _PRESET_TOOLS:
        if name in installed:
            continue
        result = subprocess.run(
            ["uv", "run", "kiso", "wrapper", "install", name],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            pytest.skip(
                f"Could not install {name}: {result.stderr[:200]}"
            )

    invalidate_wrappers_cache()
    installed = {t["name"] for t in discover_wrappers()}
    missing = [n for n in _PRESET_TOOLS if n not in installed]
    if missing:
        pytest.skip(f"Tools not available after install: {missing}")


# ---------------------------------------------------------------------------
# M1286: drive_install_flow + assert_no_command_word
# ---------------------------------------------------------------------------

# Two test-infrastructure helpers added by M1286 to make functional
# tests robust against:
# 1. LLM behavior drift on Turn 1 of an install flow (the planner is
#    free to propose, install directly, or work around — the helper
#    just keeps driving the conversation forward)
# 2. False-positive substring matches when an assertion intended to
#    catch shell commands scans free-form data fields (heredoc bodies,
#    OCR text, planner reasoning) that may incidentally contain
#    substrings matching command names (e.g. "curly" matches "curl")


async def drive_install_flow(
    run_message,
    wrapper_name: str,
    prompt: str,
    *,
    max_turns: int = 4,
    timeout: float | None = None,
):
    """Drive a conversation forward until *wrapper_name* is installed.

    Sends *prompt*, then loops sending follow-up "sì, installa il
    tool {wrapper_name}" messages until the tool is installed or
    *max_turns* is reached. When the tool finally installs, re-issues
    the original prompt one more time so the returned result reflects
    the installed-tool path.

    The helper does NOT prescribe what the planner should do on any
    given turn — it just drives the conversation forward the way a
    real user would. The planner remains free to propose installation,
    install directly, attempt a workaround, or change strategy
    mid-flow. This preserves Kiso's generalist nature in functional
    tests.

    If *max_turns* is exhausted without the tool being installed,
    returns the last result so the caller's assertion can show the
    diagnostic state.

    *timeout* defaults to ``LLM_INSTALL_TIMEOUT`` (15 min) because the
    install plan often downloads multi-hundred-MB packages and runs
    deps.sh. Caller can override with a smaller value for tests where
    the tool is already installed.
    """
    if timeout is None:
        from tests.conftest import LLM_INSTALL_TIMEOUT
        timeout = LLM_INSTALL_TIMEOUT
    kwargs = {"timeout": timeout}
    result = await run_message(prompt, **kwargs)
    turns_used = 1
    while not tool_installed(wrapper_name) and turns_used < max_turns:
        result = await run_message(
            f"sì, installa il tool {wrapper_name}", **kwargs,
        )
        turns_used += 1
    if tool_installed(wrapper_name):
        result = await run_message(prompt, **kwargs)
    return result


def assert_no_command_word(tasks, words):
    """Assert no exec task in *tasks* has a command containing any of
    *words* as a whole word.

    Only the ``command`` field of ``exec`` tasks is inspected. The
    ``detail`` field is intentionally NOT scanned because it often
    contains heredoc bodies with arbitrary stdin data (OCR output,
    API responses, planner reasoning text) that may incidentally
    contain substrings matching command names — e.g. "curly brackets"
    in OCR text would false-match "curl".

    Word boundaries (``\\b``) are used so "curly", "libcurl",
    "pycurl", "wgetopt", etc. do not trigger a match for "curl" /
    "wget".

    This helper is the recommended way to assert "the planner did
    not emit a re-download / recompile / rm-style command" in any
    functional test.
    """
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in words) + r")\b"
    )
    for t in tasks:
        if t.get("type") != "exec":
            continue
        command = t.get("command") or ""
        m = pattern.search(command)
        assert m is None, (
            f"forbidden command word {m.group(0)!r} in exec task command: "
            f"{command[:200]}"
        )
