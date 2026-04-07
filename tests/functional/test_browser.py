"""F1-F2: Browser navigation functional tests.

These tests exercise the full pipeline: user message → classifier → planner →
worker (with real browser tool) → messenger.  They require a running kiso
instance with real LLM, network access, and the browser tool available in
the registry.

When the browser tool is not pre-installed, the tests exercise the full
multi-turn install flow: first message triggers an install proposal, second
message ("sì, installa") confirms, and the agent installs + proceeds.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
    tool_installed,
    assert_url_reachable,
)

pytestmark = pytest.mark.functional

from tests.conftest import LLM_INSTALL_TIMEOUT as BROWSER_TIMEOUT

async def _run_with_install_flow(
    run_message,
    prompt: str,
    *,
    timeout: float = BROWSER_TIMEOUT,
) -> "FunctionalResult":  # noqa: F821
    """Send *prompt* and handle the install-proposal flow if needed.

    Three-turn flow when browser is not pre-installed:
      1. Original prompt → planner proposes install (msg-only plan)
      2. "sì, installa il tool browser" → planner installs the tool
      3. Repeat original prompt → planner uses the now-installed tool

    If the browser tool is already installed, returns after a single turn.
    Retries the install confirmation if the first attempt doesn't result in
    a tool appearing on disk (LLM may generate a different plan).
    """
    result = await run_message(prompt, timeout=timeout)

    if tool_installed("browser"):
        return result

    # Turn 2: confirm installation
    install_result = await run_message(
        "sì, installa il tool browser", timeout=timeout,
    )

    if not tool_installed("browser"):
        # Install may have failed or the LLM didn't execute the install.
        # Return the install result so the assertion shows what went wrong.
        return install_result

    # Turn 3: repeat original request with tool now available
    result = await run_message(prompt, timeout=timeout)
    return result


# ---------------------------------------------------------------------------
# F1 — Website description + screenshot (example.com)
# ---------------------------------------------------------------------------


class TestF1BrowserInstall:
    """F1a: Browser tool install flow."""

    async def test_browser_install_flow(self, run_message):
        """What: Trigger browser install via multi-turn approval flow.

        Why: Validates the install proposal → user approval → exec install cycle
        for the browser tool specifically. Isolates install issues from navigation.
        Expects: After the flow, the browser tool is installed and discoverable.
        """
        if tool_installed("browser"):
            pytest.skip("Browser already installed — nothing to test")

        # Turn 1: request that needs browser
        await run_message(
            "vai su example.com e dimmi cosa vedi",
            timeout=BROWSER_TIMEOUT,
        )

        if tool_installed("browser"):
            return  # installed on first turn (fast path)

        # Turn 2: confirm installation
        await run_message(
            "sì, installa il tool browser",
            timeout=BROWSER_TIMEOUT,
        )

        assert tool_installed("browser"), "Browser tool not installed after approval flow"


class TestF1BrowserNavigate:
    """F1b: Browser navigation + description (requires browser installed)."""

    async def test_navigate_and_describe(self, run_message):
        """What: Navigate to example.com and describe the page content.

        Why: Validates that the browser tool can navigate a real page and
        the messenger produces an Italian description of the content.
        example.com is IANA-maintained, no CAPTCHA, always available.
        Expects: Italian response >50 chars mentioning example/dominio/IANA.
        """
        if not tool_installed("browser"):
            pytest.skip("Browser tool not installed — run F1a first or install manually")

        result = await run_message(
            "vai su example.com e dimmi cosa c'è scritto nella pagina",
            timeout=BROWSER_TIMEOUT,
        )
        assert result.success

        output = result.last_plan_msg_output
        assert len(output) > 50, f"Too short: {output[:200]}"
        assert_no_failure_language(output)
        # example.com has minimal content — short Italian responses
        # may have fewer Italian function words than English content words.
        # Check for at least 1 Italian word instead of full assert_italian.
        _italian_words = {"il", "la", "di", "che", "è", "un", "per", "in", "con", "non", "una"}
        lower_words = set(output.lower().split())
        assert lower_words & _italian_words, f"No Italian detected: {output[:200]}"

        lower = output.lower()
        assert any(
            kw in lower
            for kw in ("example", "dominio", "iana", "illustrativ", "documentazione", "esempio")
        ), f"No relevant keywords: {output[:300]}"


class TestF1BrowserScreenshot:
    """F1c: Browser screenshot + publish (requires browser installed)."""

    async def test_screenshot_and_publish(self, run_message, func_app_client):
        """What: Take a screenshot of example.com and publish it.

        Why: Validates screenshot capture and the pub file delivery pipeline.
        Expects: .png file published with a reachable URL (>10KB).
        """
        if not tool_installed("browser"):
            pytest.skip("Browser tool not installed — run F1a first or install manually")

        result = await run_message(
            "vai su example.com e mandami uno screenshot della pagina",
            timeout=BROWSER_TIMEOUT,
        )
        assert result.success

        assert result.has_published_file("*.png"), (
            f"No .png file published. Pub files: {result.pub_files}"
        )

        for pf in result.pub_files:
            if pf["filename"].endswith(".png"):
                await assert_url_reachable(
                    pf["url"],
                    client=func_app_client,
                    expected_type="image",
                    min_size=10_000,
                )


# ---------------------------------------------------------------------------
# F2 — Wikipedia lookup (deterministic content)
# ---------------------------------------------------------------------------


class TestF2WikipediaPython:
    """Visit Wikipedia and look up what Python is."""

    async def test_wikipedia_lookup(self, run_message):
        """What: Navigate to the Python Wikipedia page and ask what Python is.

        Why: Validates that the browser tool can navigate to a stable, well-known
        URL and extract factual information. Wikipedia is always reachable, has
        structured content, and "Python" appears in any reasonable summary.
        Deterministic target avoids fragile dynamic-content assertions.
        Expects: Plan succeeds, response contains "python" (case-insensitive).
        """
        result = await _run_with_install_flow(
            run_message,
            "go to https://en.wikipedia.org/wiki/Python_(programming_language) "
            "and tell me in a few words what Python is",
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        output = result.last_plan_msg_output
        assert_no_failure_language(output)
        assert "python" in output.lower(), (
            f"Expected 'python' in response, got: {output[:400]}"
        )
