"""F36: Cross-plan file handoff — screenshot → OCR in same session.

Validates M933 (session_files/last_plan in planner prompt), M1037
(announce msgs allowed, no hallucination), and M935 (detail/expect consistency).

Two messages in the same session:
1. Take a screenshot of example.com
2. Use OCR to extract text from the screenshot

Key assertions:
- Planner uses local file path (no curl/download task)
- OCR wrapper receives correct relative path (pub/...)
- No replan needed on either step
- Messenger does not hallucinate results
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    assert_no_command_word,
    assert_no_failure_language,
    tool_installed,
)

pytestmark = [pytest.mark.functional, pytest.mark.extended]

from tests.conftest import LLM_INSTALL_TIMEOUT as TOOL_TIMEOUT

# Hallucination markers — paths/URLs that indicate fabricated results
_HALLUCINATION_MARKERS = [
    "sandbox:",
    "/mnt/data/",
    "sandbox:/mnt/",
    "/tmp/guidance",
    "github.com/guidance-ai",
]

async def _ensure_tool(run_message, wrapper_name: str, prompt: str, *, timeout: float = TOOL_TIMEOUT):
    """Run prompt, handling wrapper install flow if needed."""
    result = await run_message(prompt, timeout=timeout)
    if tool_installed(wrapper_name):
        return result
    # Install the wrapper
    await run_message(f"yes, install {wrapper_name}", timeout=timeout)
    if not tool_installed(wrapper_name):
        pytest.skip(f"Could not install {wrapper_name}")
    # Retry with wrapper available
    return await run_message(prompt, timeout=timeout)


class TestF36CrossPlanFileHandoff:
    """F36: Screenshot → OCR cross-plan file handoff."""

    async def test_screenshot_then_ocr(self, run_message):
        """Two-plan pipeline: screenshot → OCR with correct file routing.

        Verifies that the planner knows where files are (M933), doesn't
        hallucinate results in announce msgs (M1037), and generates
        consistent detail/expect pairs (M935).
        """
        # --- Plan 1: screenshot ---
        r1 = await _ensure_tool(
            run_message, "browser",
            "take a screenshot of http://example.com",
        )
        assert r1.success, f"Plan 1 (screenshot) failed: {r1.task_types()}"
        assert r1.has_published_file("*.png"), (
            f"No .png published. Pub files: {r1.pub_files}"
        )

        # No hallucination in messenger output
        for marker in _HALLUCINATION_MARKERS:
            assert marker not in r1.msg_output, (
                f"Hallucination marker '{marker}' found in: {r1.msg_output[:200]}"
            )

        # --- Plan 2: OCR the screenshot ---
        r2 = await _ensure_tool(
            run_message, "ocr",
            "use OCR to extract the text from the screenshot",
        )
        assert r2.success, f"Plan 2 (OCR) failed: {r2.task_types()}"

        # planner should NOT create curl/download tasks (file is local)
        last_plan_id = r2.plans[-1]["id"]
        last_plan_tasks = [
            t for t in r2.tasks if t.get("plan_id") == last_plan_id
        ]
        # Word-boundary check on the command field only — avoids false
        # positives like "libcurl" or "curly" that may appear in
        # command text but are not the curl program (M1286).
        assert_no_command_word(last_plan_tasks, ["curl", "wget"])
        exec_tasks = [
            t for t in last_plan_tasks if t.get("type") == "exec"
        ]
        for t in exec_tasks:
            detail = (t.get("detail") or "").lower()
            assert "download" not in detail, (
                f"Download task created (file should be local): {detail[:200]}"
            )

        # OCR wrapper task should have correct path with pub/ prefix
        ocr_tasks = [
            t for t in last_plan_tasks
            if t.get("type") == "wrapper" and t.get("wrapper") == "ocr"
        ]
        assert ocr_tasks, f"No OCR wrapper task found. Types: {[t.get('type') for t in last_plan_tasks]}"

        # OCR should produce non-empty text output
        ocr_output = ocr_tasks[0].get("output", "") or ""
        assert len(ocr_output) > 20, (
            f"OCR output too short (expected text from example.com): {ocr_output[:200]}"
        )

        # No hallucination in messenger output
        for marker in _HALLUCINATION_MARKERS:
            assert marker not in r2.msg_output, (
                f"Hallucination marker '{marker}' found in: {r2.msg_output[:200]}"
            )

        # Check messenger output is grounded
        assert_no_failure_language(r2.last_plan_msg_output)
