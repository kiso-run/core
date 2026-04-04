"""F27-F30: Post-preset workflow tests — tools pre-installed.

These tests assume the default preset (browser, ocr, aider) is installed
before any test runs. The session-scoped fixture handles the install once.
This separates tool USAGE testing from install FLOW testing (done by F1).

Marked @pytest.mark.extended because the initial preset install is slow.
"""

from __future__ import annotations

import pytest

from tests.conftest import LLM_MULTI_PLAN_TIMEOUT
from tests.functional.conftest import (
    FunctionalResult,
    assert_no_failure_language,
)

pytestmark = [pytest.mark.functional, pytest.mark.extended]


# ---------------------------------------------------------------------------
# F27 — Browse and describe a website
# ---------------------------------------------------------------------------


class TestF27BrowseAndDescribe:
    """Browse a website and describe its content — browser tool only."""

    async def test_browse_and_describe(self, preset_tools_installed, run_message):
        """What: Navigates to example.com and describes the page.

        Why: Validates browser tool works end-to-end without install flow.
        Expects: Success, Italian response, mentions 'example' or 'domain'.
        """
        result = await run_message(
            "vai su example.com e dimmi cosa c'è scritto nella pagina",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed: {[p.get('status') for p in result.plans]}"
        )
        # Don't assert_italian — response may contain quoted English web content.
        # Language compliance is tested by F12 (messenger quality).
        assert_no_failure_language(result.last_plan_msg_output)
        tool_names = [
            FunctionalResult.task_tool_name(t) for t in result.tasks
            if t.get("type") == "tool"
        ]
        assert "browser" in tool_names, f"Browser not used: {tool_names}"

        output = result.last_plan_msg_output.lower()
        assert any(w in output for w in (
            "example", "dominio", "iana", "illustrativo", "domain",
        )), f"No example.com keywords in output: {output[:300]}"


# ---------------------------------------------------------------------------
# F28 — Screenshot + OCR text extraction
# ---------------------------------------------------------------------------


class TestF28ScreenshotOCR:
    """Take screenshot and extract text — browser + ocr pipeline."""

    async def test_screenshot_and_ocr(self, preset_tools_installed, run_message):
        """What: Screenshots example.com and extracts text via OCR.

        Why: Validates browser→ocr cross-tool pipeline and file routing (M826).
        Expects: Success, screenshot published, OCR output mentions 'example'.
        """
        result = await run_message(
            "fai uno screenshot di example.com ed estrai il testo dalla pagina",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed: {[p.get('status') for p in result.plans]}"
        )

        # Should have used both browser and ocr tools
        tool_names = [
            FunctionalResult.task_tool_name(t) for t in result.tasks
            if t.get("type") == "tool"
        ]
        assert "browser" in tool_names, f"Browser not used: {tool_names}"
        assert result.has_published_file("*.png"), (
            f"Expected published screenshot artifact, got: {result.pub_files}"
        )

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

    async def test_aider_write_script(self, preset_tools_installed, run_message):
        """What: Asks aider to write a hello.py script.

        Why: Validates aider tool works for code generation.
        Expects: Success, aider tool task used.
        """
        result = await run_message(
            "usa aider per scrivere uno script hello.py che stampa 'ciao mondo'",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed: {[p.get('status') for p in result.plans]}"
        )

        # Aider should have been used as a tool task
        aider_tasks = [
            t for t in result.tasks
            if t.get("type") == "tool"
            and FunctionalResult.task_tool_name(t) == "aider"
        ]
        assert aider_tasks, (
            f"Expected aider tool task, got types: {result.task_types()}"
        )
        task_blob = "\n".join(
            (t.get("detail") or "") + "\n" + (t.get("command") or "")
            for t in result.tasks
        ).lower()
        assert "hello.py" in task_blob, (
            f"Expected workflow to reference hello.py, got: {task_blob[:400]}"
        )


# ---------------------------------------------------------------------------
# F30 — Full pipeline: browse → OCR → aider → exec → msg
# ---------------------------------------------------------------------------


class TestF30FullPipeline:
    """Full multi-tool pipeline without install flow fragility."""

    async def test_browse_ocr_aider_exec(self, preset_tools_installed, run_message):
        """What: Screenshot + OCR, then write + run word count script.

        Why: Replaces F17 — same coverage but tools pre-installed, no install
        flow fragility. Tests cross-plan file awareness and tool orchestration.

        Plan 1: screenshot example.com + OCR text extraction
        Plan 2: aider writes word_count script + exec runs it
        """
        # Plan 1: screenshot + OCR
        r1 = await run_message(
            "fai screenshot di https://en.wikipedia.org/wiki/Python_(programming_language), estrai il testo con OCR "
            "e salva il testo estratto in un file",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r1.success, f"Plan 1 failed: {r1.task_types()}"
        r1_tool_names = [
            FunctionalResult.task_tool_name(t) for t in r1.tasks
            if t.get("type") == "tool"
        ]
        assert "browser" in r1_tool_names, f"Plan 1 missing browser tool: {r1_tool_names}"
        assert "ocr" in r1_tool_names, f"Plan 1 missing ocr tool: {r1_tool_names}"

        # Plan 2: write script + execute
        r2 = await run_message(
            "usa aider per scrivere uno script word_count.py che legge "
            "il testo estratto e conta le parole, poi eseguilo e dimmi "
            "il risultato",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r2.success, f"Plan 2 failed: {r2.task_types()}"
        r2_tool_names = [
            FunctionalResult.task_tool_name(t) for t in r2.tasks
            if t.get("type") == "tool"
        ]
        assert "aider" in r2_tool_names, f"Plan 2 missing aider tool: {r2_tool_names}"
        assert "exec" in r2.task_types(), f"Plan 2 missing exec task: {r2.task_types()}"

        output = r2.last_plan_msg_output
        assert_no_failure_language(output)
