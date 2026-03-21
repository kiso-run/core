"""Fixtures and helpers for functional acceptance tests.

Functional tests exercise the full kiso pipeline (classifier → planner →
worker → skills → messenger) with real LLM, real network, and real skill
execution.  They are gated by ``--functional`` and optionally
``--destructive`` pytest flags.
"""

from __future__ import annotations

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
from kiso.main import _collect_boot_facts, _init_ssh_keys
from kiso.store import create_session, init_db, save_message
from kiso.worker import _process_message


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

_LANG_WORDS = {"it": _IT_WORDS, "en": _EN_WORDS, "es": _ES_WORDS}
_LANG_NAMES = {"it": "Italian", "en": "English", "es": "Spanish"}

_WORD_RE = re.compile(r"[a-zàèéìòùáéíóúñü]+", re.IGNORECASE)

# Strip fenced code blocks so code keywords don't skew language heuristics.
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)


_LIST_ITEM_RE = re.compile(r"^\s*[-*]\s+.*$", re.MULTILINE)
_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+\.\s+.*$", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^>.*$", re.MULTILINE)


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
    """Assert that *text* is predominantly in *lang* ("it", "en", or "es").

    Compares the target language's function-word score against the highest
    non-target score.  Code blocks are stripped first.
    """
    cleaned = _strip_code_blocks(text)
    scores: dict[str, int] = {k: 0 for k in _LANG_WORDS}
    for w in _WORD_RE.findall(cleaned):
        wl = w.lower()
        for k, words in _LANG_WORDS.items():
            scores[k] += wl in words
    target = scores[lang]
    best_other = max(v for k, v in scores.items() if k != lang)
    name = _LANG_NAMES.get(lang, lang)
    assert target > best_other, (
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
        resp = await client.get(url)
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
        return [t for t in self.tasks if t.get("type") == "tool"]

    @staticmethod
    def task_tool_name(task: dict) -> str:
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
    "kiso.tools",
    "kiso.main",
    "kiso.pub",
    "kiso.log",
    "kiso.audit",
    "kiso.sysenv",
    "kiso.connectors",
    "kiso.recipe_loader",
    "kiso.tool_repair",
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

    Creates a temp KISO_DIR with clean ``tools/`` and ``sys/ssh/`` dirs,
    patches every module that imports KISO_DIR, stubs ``reload_config``
    so mid-execution config reloads return func_config, writes a config.toml
    so subprocess CLI commands can load it, and sets KISO_HOME so subprocess
    processes resolve KISO_DIR to the isolated directory.
    SSH keys are generated inside the temp dir.
    """
    kiso_dir = tmp_path_factory.mktemp("kiso_home")
    (kiso_dir / "tools").mkdir()
    (kiso_dir / "sys" / "ssh").mkdir(parents=True)

    # M543: write config.toml for subprocess CLI commands
    _write_test_config(kiso_dir, func_config)

    # M543: set KISO_HOME so subprocess processes use the isolated dir
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
            "user_tools": "*",
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
            ),
            timeout=timeout,
        )
        # Wait for background knowledge task (curator + summarizer).
        # M634: increased from 30s to 90s — curator with transport retries
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
