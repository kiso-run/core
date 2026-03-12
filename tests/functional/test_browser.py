"""F1-F2: Browser navigation functional tests.

These tests exercise the full pipeline: user message → classifier → planner →
worker (with real browser skill) → messenger.  They require a running kiso
instance with real LLM, network access, and the browser skill available in
the registry.
"""

from __future__ import annotations

import pytest

from kiso.config import KISO_DIR
from kiso.tools import discover_tools
from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
    assert_url_reachable,
)

pytestmark = pytest.mark.functional

BROWSER_TIMEOUT = 300  # browser install + navigation can be slow


def _browser_installed() -> bool:
    """Check if the browser tool is installed."""
    return any(t["name"] == "browser" for t in discover_tools())


_skip_no_browser = pytest.mark.skipif(
    not _browser_installed(),
    reason="browser tool not installed",
)


# ---------------------------------------------------------------------------
# F1 — Website description + screenshot (guidance.studio)
# ---------------------------------------------------------------------------


@_skip_no_browser
class TestF1GuidanceStudioScreenshot:
    """Visit guidance.studio, describe the company, and take a screenshot."""

    async def test_website_description_and_screenshot(self, run_message):
        result = await run_message(
            "vai su guidance.studio, dimmi di cosa si occupa questa azienda "
            "sulla base delle info nel sito, e poi mi mandi uno screenshot della home",
            timeout=BROWSER_TIMEOUT,
        )

        # Plan completed successfully
        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian and substantial
        assert len(result.msg_output) > 100, (
            f"msg output too short ({len(result.msg_output)} chars): "
            f"{result.msg_output[:200]}"
        )
        assert_italian(result.msg_output)
        assert_no_failure_language(result.msg_output)

        # Response mentions something relevant about the company
        lower = result.msg_output.lower()
        assert any(
            kw in lower
            for kw in ("guidance", "studio", "azienda", "company", "software", "serviz")
        ), f"No relevant keywords in output: {result.msg_output[:300]}"

        # Screenshot was published
        assert result.has_published_file("*.png"), (
            f"No .png file published. Pub files: {result.pub_files}"
        )

        # Published screenshot URLs are reachable
        for pf in result.pub_files:
            if pf["filename"].endswith(".png"):
                await assert_url_reachable(
                    pf["url"],
                    expected_type="image",
                    min_size=10_000,  # real screenshot > 10KB
                )


# ---------------------------------------------------------------------------
# F2 — News extraction (gazzetta.it)
# ---------------------------------------------------------------------------


@_skip_no_browser
class TestF2GazzettaNews:
    """Visit gazzetta.it and extract latest news."""

    async def test_news_extraction(self, run_message):
        result = await run_message(
            "vai su gazzetta.it e dimmi quali sono le ultime notizie",
            timeout=BROWSER_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian and substantial
        assert_italian(result.msg_output)
        assert_no_failure_language(result.msg_output)
        assert len(result.msg_output) > 200, (
            f"msg output too short ({len(result.msg_output)} chars) — "
            f"expected multiple news items: {result.msg_output[:300]}"
        )

        # Response contains multiple items (line breaks or list patterns)
        lines = [ln for ln in result.msg_output.strip().splitlines() if ln.strip()]
        assert len(lines) >= 3, (
            f"Expected at least 3 lines of news, got {len(lines)}: "
            f"{result.msg_output[:300]}"
        )

        # At least one sports/news keyword (Gazzetta dello Sport)
        lower = result.msg_output.lower()
        news_keywords = (
            "notizi", "sport", "calcio", "serie", "campionato",
            "partita", "gol", "risultat", "classifica", "squadra",
            "giocator", "allenator", "trasferim", "champions",
        )
        assert any(kw in lower for kw in news_keywords), (
            f"No news/sports keywords found in output: {result.msg_output[:400]}"
        )
