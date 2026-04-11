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
        wrapper_names = [
            FunctionalResult.task_wrapper_name(t) for t in result.tasks
            if t.get("type") == "tool"
        ]
        assert "browser" in wrapper_names, f"Browser not used: {wrapper_names}"

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
        wrapper_names = [
            FunctionalResult.task_wrapper_name(t) for t in result.tasks
            if t.get("type") == "tool"
        ]
        assert "browser" in wrapper_names, f"Browser not used: {wrapper_names}"
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
    """Write a non-trivial Python module using aider tool.

    M1304: the original test asked for a one-liner hello.py, which the
    planner legitimately handled via exec (echo "print(...)"). The prompt
    now requests a multi-method class with real logic — complex enough
    that aider is the natural tool choice over exec.
    """

    async def test_aider_write_script(self, preset_tools_installed, run_message):
        """What: Asks aider to create a Calculator class with four methods.

        Why: Validates aider tool works for non-trivial code generation.
        A multi-method class with error handling (division by zero) is
        complex enough that the planner should choose aider over exec.
        Expects: Success, aider tool task used, calculator.py referenced
        in task details.
        """
        result = await run_message(
            "usa aider per creare calculator.py con una classe Calculator "
            "che abbia metodi add, subtract, multiply e divide. "
            "Il metodo divide deve gestire la divisione per zero con un "
            "ValueError. Non eseguire il file e non aggiungere test.",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed: {[p.get('status') for p in result.plans]}"
        )

        # Aider should have been used as a tool task
        aider_tasks = [
            t for t in result.tasks
            if t.get("type") == "tool"
            and FunctionalResult.task_wrapper_name(t) == "aider"
        ]
        assert aider_tasks, (
            f"Expected aider tool task, got types: {result.task_types()}"
        )
        task_blob = "\n".join(
            (t.get("detail") or "") + "\n" + (t.get("command") or "")
            for t in result.tasks
        ).lower()
        assert "calculator" in task_blob, (
            f"Expected workflow to reference calculator, got: {task_blob[:400]}"
        )


# ---------------------------------------------------------------------------
# F30 — Full pipeline: browse → OCR → aider → exec → msg
# ---------------------------------------------------------------------------


class TestF30FullPipeline:
    """Full multi-tool pipeline without install flow fragility."""

    async def test_browse_ocr_aider_exec(self, preset_tools_installed, run_message):
        """What: Screenshot + OCR, then write + run a deterministic text-stats script.

        Why: Replaces F17 — same coverage but tools pre-installed, no install
        flow fragility. Tests cross-plan file awareness and tool orchestration.

        Plan 1: screenshot + OCR text extraction
        Plan 2: aider writes text_stats script + exec runs it
        """
        # Plan 1: screenshot + OCR
        r1 = await run_message(
            "fai screenshot di https://en.wikipedia.org/wiki/Python_(programming_language), estrai il testo con OCR "
            "e salva il testo estratto in un file",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r1.success, f"Plan 1 failed: {r1.task_types()}"
        r1_wrapper_names = [
            FunctionalResult.task_wrapper_name(t) for t in r1.tasks
            if t.get("type") == "tool"
        ]
        assert "browser" in r1_wrapper_names, f"Plan 1 missing browser tool: {r1_wrapper_names}"
        assert "ocr" in r1_wrapper_names, f"Plan 1 missing ocr tool: {r1_wrapper_names}"

        # Plan 2: write script + execute
        r2 = await run_message(
            "usa aider per scrivere uno script text_stats.py che legge testo da stdin "
            "e stampa esattamente due righe nel formato 'chars: N' e 'lines: N', "
            "poi eseguilo sul testo estratto e dimmi il risultato",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r2.success, f"Plan 2 failed: {r2.task_types()}"
        r2_wrapper_names = [
            FunctionalResult.task_wrapper_name(t) for t in r2.tasks
            if t.get("type") == "tool"
        ]
        assert "aider" in r2_wrapper_names, f"Plan 2 missing aider tool: {r2_wrapper_names}"
        assert "exec" in r2.task_types(), f"Plan 2 missing exec task: {r2.task_types()}"

        output = r2.last_plan_msg_output.lower()
        assert_no_failure_language(output)
        from tests.functional.conftest import CHARS_COUNT_RE, LINES_COUNT_RE
        assert CHARS_COUNT_RE.search(output), f"Missing chars count: {output[:500]}"
        assert LINES_COUNT_RE.search(output), f"Missing lines count: {output[:500]}"
