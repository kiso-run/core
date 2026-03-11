"""Fixtures and helpers for functional acceptance tests.

Functional tests exercise the full kiso pipeline (classifier → planner →
worker → skills → messenger) with real LLM, real network, and real skill
execution.  They are gated by ``--functional`` and optionally
``--destructive`` pytest flags.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio

from kiso.config import (
    Config,
    MODEL_DEFAULTS,
    Provider,
    SETTINGS_DEFAULTS,
    User,
)
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

_WORD_RE = re.compile(r"[a-zàèéìòù]+", re.IGNORECASE)


def assert_italian(text: str) -> None:
    """Assert that *text* is predominantly Italian (not English).

    Uses a simple keyword-frequency heuristic: counts occurrences of common
    Italian vs English function words.  Raises ``AssertionError`` with both
    scores if Italian does not win.
    """
    it_score = en_score = 0
    for w in _WORD_RE.findall(text):
        w = w.lower()
        it_score += w in _IT_WORDS
        en_score += w in _EN_WORDS
    assert it_score > en_score, (
        f"Text does not appear to be Italian (IT={it_score}, EN={en_score}). "
        f"First 200 chars: {text[:200]}"
    )


# ---------------------------------------------------------------------------
# URL reachability helper
# ---------------------------------------------------------------------------


async def assert_url_reachable(
    url: str,
    *,
    expected_type: str | None = None,
    min_size: int = 1,
    timeout: float = 30,
) -> None:
    """Assert that *url* is reachable (HTTP 200) and optionally check Content-Type.

    Parameters
    ----------
    url:
        Full URL to GET.
    expected_type:
        If provided, assert ``Content-Type`` starts with this string
        (e.g. ``"image"``).
    min_size:
        Minimum response body size in bytes.
    timeout:
        HTTP request timeout in seconds.
    """
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        resp = await client.get(url)
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
    """Assert that *text* does not contain obvious failure indicators."""
    match = _FAILURE_PATTERNS.search(text)
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

    def has_published_file(self, glob_pattern: str) -> bool:
        """Check if any published file matches *glob_pattern*."""
        return any(
            fnmatch.fnmatch(f.get("filename", ""), glob_pattern)
            for f in self.pub_files
        )

    def task_types(self) -> list[str]:
        """Return list of task types in execution order."""
        return [t.get("type", "?") for t in self.tasks]

    def skill_tasks(self) -> list[dict]:
        """Return only skill-type tasks."""
        return [t for t in self.tasks if t.get("type") == "skill"]


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
        users={"testadmin": User(role="admin")},
        models={**MODEL_DEFAULTS},
        settings=settings,
        raw={},
    )


# ---------------------------------------------------------------------------
# Per-test DB and session
# ---------------------------------------------------------------------------


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

    async def _run(
        content: str,
        *,
        timeout: float = 300,
        base_url: str = "",
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
        # Wait for background knowledge task if any
        if bg_task is not None and not bg_task.done():
            try:
                await asyncio.wait_for(bg_task, timeout=30)
            except (asyncio.TimeoutError, Exception):
                pass

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
