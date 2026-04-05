"""F17: Multi-tool pipeline — browser → ocr → aider → exec → msg.

Exercises the full cross-tool pipeline in a single session:
1. Navigate to Wikipedia Python page and take a screenshot (browser)
2. Extract text from the screenshot (ocr)
3. Write a Python word count script using aider
4. Run the script (exec)
5. Deliver results to the user (msg)

Validates M822 (session file listing), M823 (cross-plan state),
M824 (tool injection), M825/M826 (file routing).
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    assert_no_failure_language,
    tool_installed,
)

pytestmark = [pytest.mark.functional, pytest.mark.extended]

from tests.conftest import LLM_INSTALL_TIMEOUT as TOOL_TIMEOUT

async def _run_with_tool_install(
    run_message,
    tool_name: str,
    prompt: str,
    *,
    timeout: float = TOOL_TIMEOUT,
):
    """Send *prompt* and handle the install-proposal flow if tool is missing.

    Uses the shared conversational install flow, generalized for any tool.

    Three-turn flow when tool is not pre-installed:
      1. Original prompt → planner proposes install (msg-only plan)
      2. "sì, installa il tool {name}" → planner installs the tool
      3. Repeat original prompt → planner uses the now-installed tool

    If the tool is already installed, returns after a single turn.
    """
    result = await run_message(prompt, timeout=timeout)

    if tool_installed(tool_name):
        return result

    # Turn 2: confirm installation
    install_result = await run_message(
        f"sì, installa il tool {tool_name}",
        timeout=timeout,
    )

    if not tool_installed(tool_name):
        return install_result

    # Turn 3: repeat original request with tool now available
    return await run_message(prompt, timeout=timeout)


# ---------------------------------------------------------------------------
# F17 — Multi-tool pipeline: browser → ocr → aider → exec → msg
# ---------------------------------------------------------------------------


class TestF17FullPipeline:
    """F17: Full multi-tool pipeline — browser → ocr → aider → exec → msg.

    Each step is a separate plan in the same session, exercising cross-plan
    file awareness (M822-M826). Tools are installed via the standard
    conversational install flow (same as F1) if not already present.
    """

    async def test_screenshot_ocr_aider_exec_msg(self, run_message):
        """What: 4-plan pipeline: screenshot → OCR → aider script → exec+msg.

        Why: End-to-end validation that the planner discovers files from
        prior plans, routes them to the correct tool (via consumes metadata),
        and uses aider for code generation (not exec).
        Expects: Final message contains deterministic text stats from OCR text.
        """
        # --- Plan 1: screenshot (installs browser if needed) ---
        r1 = await _run_with_tool_install(
            run_message, "browser",
            "Navigate to https://en.wikipedia.org/wiki/Python_(programming_language) and take a screenshot of the page",
        )
        assert r1.success, f"Plan 1 (screenshot) failed: {r1.task_types()}"
        assert r1.has_published_file("*.png"), (
            f"No .png published. Pub files: {r1.pub_files}"
        )

        # --- Plan 2: OCR the screenshot (installs ocr if needed) ---
        r2 = await _run_with_tool_install(
            run_message, "ocr",
            "Extract the text from the screenshot using OCR",
        )
        assert r2.success, f"Plan 2 (OCR) failed: {r2.task_types()}"

        # Verify OCR found example.com content (filter to OCR tool tasks only,
        # not msg tasks that might mention "example" without actual extraction)
        last_plan_id = r2.plans[-1]["id"]
        ocr_tool_outputs = [
            t.get("output", "") or ""
            for t in r2.tasks
            if t.get("type") == "tool" and t.get("plan_id") == last_plan_id
            and t.get("status") == "done"
        ]
        assert ocr_tool_outputs, (
            f"No OCR tool tasks in last plan. Types: {r2.task_types()}"
        )
        ocr_output = " ".join(ocr_tool_outputs).lower()
        assert "python" in ocr_output, (
            f"OCR output missing 'python': {ocr_output[:500]}"
        )

        # --- Plan 3: write text stats script with aider (installs aider if needed) ---
        r3 = await _run_with_tool_install(
            run_message, "aider",
            "Use aider to create a Python script text_stats.py that reads "
            "text from stdin and prints exactly two lines: 'chars: N' and "
            "'lines: N'. Create only the file. Do not run it and do not add tests.",
        )
        assert r3.success, f"Plan 3 (aider) failed: {r3.task_types()}"

        # Verify aider was used (tool task, not exec)
        aider_tasks = [
            t for t in r3.tasks
            if t.get("type") == "tool"
            and (t.get("skill") == "aider" or t.get("tool") == "aider")
        ]
        assert aider_tasks, (
            f"Expected aider tool task, got types: {r3.task_types()}"
        )
        last_plan_id = r3.plans[-1]["id"]
        last_plan_task_types = [
            t.get("type") for t in r3.tasks if t.get("plan_id") == last_plan_id
        ]
        assert "exec" not in last_plan_task_types, (
            f"Plan 3 should stay codegen-only, got: {last_plan_task_types}"
        )

        # --- Plan 4: run script + deliver results ---
        r4 = await run_message(
            "Run text_stats.py with the OCR text as input and "
            "send me the result",
            timeout=TOOL_TIMEOUT,
        )
        assert r4.success, f"Plan 4 (exec+msg) failed: {r4.task_types()}"
        last_plan_id = r4.plans[-1]["id"]
        last_plan_tasks = [t for t in r4.tasks if t.get("plan_id") == last_plan_id]
        task_blob = "\n".join(
            ((t.get("detail") or "") + "\n" + (t.get("command") or ""))
            for t in last_plan_tasks
        ).lower()
        assert "text_stats.py" in task_blob, (
            f"Expected plan 4 to reuse generated script path, got: {task_blob[:500]}"
        )
        assert "curl" not in task_blob and "wget" not in task_blob, (
            f"Unexpected re-download instead of local path reuse: {task_blob[:500]}"
        )

        output = r4.last_plan_msg_output
        assert len(output) > 20, f"Output too short: {output}"
        assert_no_failure_language(output)

        # Should contain deterministic text stats
        import re
        assert re.search(r"chars:\s*\d+", output.lower()), f"Missing chars count: {output[:500]}"
        assert re.search(r"lines:\s*\d+", output.lower()), f"Missing lines count: {output[:500]}"
