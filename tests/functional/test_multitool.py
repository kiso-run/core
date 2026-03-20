"""F17: Multi-tool pipeline — browser → ocr → aider → exec → msg.

Exercises the full cross-tool pipeline in a single session:
1. Navigate to example.com and take a screenshot (browser)
2. Extract text from the screenshot (ocr)
3. Write a Python word count script using aider
4. Run the script (exec)
5. Deliver results to the user (msg)

Validates M822 (session file listing), M823 (cross-plan state),
M824 (tool injection), M825/M826 (file routing).
"""

from __future__ import annotations

import pytest

from kiso.tools import discover_tools, invalidate_tools_cache
from tests.functional.conftest import (
    assert_no_failure_language,
)

pytestmark = pytest.mark.functional

from tests.conftest import LLM_INSTALL_TIMEOUT as TOOL_TIMEOUT


def _tool_installed(name: str) -> bool:
    """Check if a specific tool is installed."""
    invalidate_tools_cache()
    return any(t["name"] == name for t in discover_tools())


async def _ensure_tool(run_message, name: str) -> None:
    """Install a tool via the conversational install flow if not present."""
    if _tool_installed(name):
        return

    # Turn 1: ask for something that needs the tool → triggers install proposal
    await run_message(
        f"I need to use the {name} tool. Please install it.",
        timeout=TOOL_TIMEOUT,
    )

    if _tool_installed(name):
        return

    # Turn 2: explicit install confirmation
    await run_message(
        f"Yes, install the {name} tool now",
        timeout=TOOL_TIMEOUT,
    )

    if not _tool_installed(name):
        pytest.skip(f"Could not install tool '{name}' via LLM flow")


# ---------------------------------------------------------------------------
# F17 — Multi-tool pipeline: browser → ocr → aider → exec → msg
# ---------------------------------------------------------------------------


class TestF17FullPipeline:
    """F17: Full multi-tool pipeline — browser → ocr → aider → exec → msg.

    Each step is a separate plan in the same session, exercising cross-plan
    file awareness (M822-M826). Tools are installed via the conversational
    flow if not already present.
    """

    async def test_screenshot_ocr_aider_exec_msg(self, run_message):
        """What: 5-plan pipeline: screenshot → OCR → aider script → exec → msg.

        Why: End-to-end validation that the planner discovers files from
        prior plans, routes them to the correct tool (via consumes metadata),
        and uses aider for code generation (not exec).
        Expects: Final message contains word frequency data from example.com.
        """
        # --- Ensure required tools are installed ---
        for tool in ("browser", "ocr", "aider"):
            await _ensure_tool(run_message, tool)

        # --- Plan 1: take screenshot ---
        r1 = await run_message(
            "Navigate to http://example.com and take a screenshot of the page",
            timeout=TOOL_TIMEOUT,
        )
        assert r1.success, f"Plan 1 (screenshot) failed: {r1.task_types()}"
        assert r1.has_published_file("*.png"), (
            f"No .png published. Pub files: {r1.pub_files}"
        )

        # --- Plan 2: OCR the screenshot ---
        r2 = await run_message(
            "Extract the text from the screenshot using OCR",
            timeout=TOOL_TIMEOUT,
        )
        assert r2.success, f"Plan 2 (OCR) failed: {r2.task_types()}"

        # Verify OCR found example.com content
        ocr_output = " ".join(
            t.get("output", "") or "" for t in r2.tasks
            if t.get("status") == "done"
        ).lower()
        assert "example" in ocr_output, (
            f"OCR output missing 'example': {ocr_output[:500]}"
        )

        # --- Plan 3: write word count script with aider ---
        r3 = await run_message(
            "Use aider to write a Python script word_count.py that reads "
            "text from stdin and prints each word with its count, sorted "
            "by frequency descending. Format: 'word: N' one per line.",
            timeout=TOOL_TIMEOUT,
        )
        assert r3.success, f"Plan 3 (aider) failed: {r3.task_types()}"

        # Verify aider was used (tool task, not exec)
        aider_tasks = [
            t for t in r3.tasks
            if t.get("type") == "tool" and (t.get("skill") == "aider" or t.get("tool") == "aider")
        ]
        assert aider_tasks, (
            f"Expected aider tool task, got types: {r3.task_types()}"
        )

        # --- Plan 4: run script + deliver results ---
        r4 = await run_message(
            "Run word_count.py with the OCR text as input and "
            "send me the top 10 most frequent words",
            timeout=TOOL_TIMEOUT,
        )
        assert r4.success, f"Plan 4 (exec+msg) failed: {r4.task_types()}"

        output = r4.last_plan_msg_output
        assert len(output) > 20, f"Output too short: {output}"
        assert_no_failure_language(output)

        # Should contain word frequency data from example.com
        lower = output.lower()
        assert any(
            kw in lower
            for kw in ("example", "domain", "document", "information", "use")
        ), f"Expected example.com keywords in word counts: {output[:500]}"
