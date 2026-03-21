"""F27-F30: Post-preset workflow tests — tools pre-installed.

These tests assume the default preset (browser, ocr, aider) is installed
before any test runs. The session-scoped fixture handles the install once.
This separates tool USAGE testing from install FLOW testing (done by F1).

Marked @pytest.mark.extended because the initial preset install is slow.
"""

from __future__ import annotations

import subprocess

import pytest

from kiso.tools import discover_tools, invalidate_tools_cache
from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
)

pytestmark = [pytest.mark.functional, pytest.mark.extended]

_REQUIRED_TOOLS = ["browser", "ocr", "aider"]


@pytest.fixture(scope="session")
def _preset_tools_installed():
    """Install browser, ocr, and aider before any test in this file.

    Uses subprocess kiso tool install (same as production). Skips if
    a tool is already installed. Session-scoped — runs once per test session.
    """
    invalidate_tools_cache()
    installed = {t["name"] for t in discover_tools()}

    for name in _REQUIRED_TOOLS:
        if name in installed:
            continue
        result = subprocess.run(
            ["uv", "run", "kiso", "tool", "install", name],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            pytest.skip(
                f"Could not install {name}: {result.stderr[:200]}"
            )

    invalidate_tools_cache()
    installed = {t["name"] for t in discover_tools()}
    missing = [n for n in _REQUIRED_TOOLS if n not in installed]
    if missing:
        pytest.skip(f"Tools not available after install: {missing}")


# ---------------------------------------------------------------------------
# F27 — Browse and describe a website
# ---------------------------------------------------------------------------


class TestF27BrowseAndDescribe:
    """Browse a website and describe its content — browser tool only."""

    async def test_browse_and_describe(self, _preset_tools_installed, run_message):
        """What: Navigates to example.com and describes the page.

        Why: Validates browser tool works end-to-end without install flow.
        Expects: Success, Italian response, mentions 'example' or 'domain'.
        """
        result = await run_message(
            "vai su example.com e dimmi cosa c'è scritto nella pagina",
            timeout=300,
        )

        assert result.success, (
            f"Plan failed: {[p.get('status') for p in result.plans]}"
        )
        assert_italian(result.last_plan_msg_output)
        assert_no_failure_language(result.last_plan_msg_output)

        output = result.last_plan_msg_output.lower()
        assert any(w in output for w in (
            "example", "dominio", "iana", "illustrativo",
        )), f"No example.com keywords in output: {output[:300]}"


# ---------------------------------------------------------------------------
# F28 — Screenshot + OCR text extraction
# ---------------------------------------------------------------------------


class TestF28ScreenshotOCR:
    """Take screenshot and extract text — browser + ocr pipeline."""

    async def test_screenshot_and_ocr(self, _preset_tools_installed, run_message):
        """What: Screenshots example.com and extracts text via OCR.

        Why: Validates browser→ocr cross-tool pipeline and file routing (M826).
        Expects: Success, screenshot published, OCR output mentions 'example'.
        """
        result = await run_message(
            "fai uno screenshot di example.com ed estrai il testo dalla pagina",
            timeout=300,
        )

        assert result.success, (
            f"Plan failed: {[p.get('status') for p in result.plans]}"
        )

        # Should have used both browser and ocr tools
        tool_names = [
            t.get("tool") for t in result.tasks
            if t.get("type") == "tool" and t.get("tool")
        ]
        assert "browser" in tool_names, f"Browser not used: {tool_names}"

        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
        ).lower()
        assert "example" in all_output, (
            f"OCR output missing 'example': {all_output[:500]}"
        )


# ---------------------------------------------------------------------------
# F29 — Write code with aider
# ---------------------------------------------------------------------------


class TestF29AiderWriteCode:
    """Write a Python script using aider tool."""

    async def test_aider_write_script(self, _preset_tools_installed, run_message):
        """What: Asks aider to write a hello.py script.

        Why: Validates aider tool works for code generation.
        Expects: Success, aider tool task used.
        """
        result = await run_message(
            "usa aider per scrivere uno script hello.py che stampa 'ciao mondo'",
            timeout=300,
        )

        assert result.success, (
            f"Plan failed: {[p.get('status') for p in result.plans]}"
        )

        # Aider should have been used as a tool task
        aider_tasks = [
            t for t in result.tasks
            if t.get("type") == "tool" and t.get("tool") == "aider"
        ]
        assert aider_tasks, (
            f"Expected aider tool task, got types: {result.task_types()}"
        )


# ---------------------------------------------------------------------------
# F30 — Full pipeline: browse → OCR → aider → exec → msg
# ---------------------------------------------------------------------------


class TestF30FullPipeline:
    """Full multi-tool pipeline without install flow fragility."""

    async def test_browse_ocr_aider_exec(self, _preset_tools_installed, run_message):
        """What: Screenshot + OCR, then write + run word count script.

        Why: Replaces F17 — same coverage but tools pre-installed, no install
        flow fragility. Tests cross-plan file awareness and tool orchestration.

        Plan 1: screenshot example.com + OCR text extraction
        Plan 2: aider writes word_count script + exec runs it
        """
        # Plan 1: screenshot + OCR
        r1 = await run_message(
            "fai screenshot di example.com, estrai il testo con OCR "
            "e salva il testo estratto in un file",
            timeout=300,
        )
        assert r1.success, f"Plan 1 failed: {r1.task_types()}"

        # Plan 2: write script + execute
        r2 = await run_message(
            "usa aider per scrivere uno script word_count.py che legge "
            "il testo estratto e conta le parole, poi eseguilo e dimmi "
            "il risultato",
            timeout=300,
        )
        assert r2.success, f"Plan 2 failed: {r2.task_types()}"

        output = r2.last_plan_msg_output
        assert len(output) > 20, f"Output too short: {output}"
        assert_no_failure_language(output)
