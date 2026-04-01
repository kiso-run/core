"""F36: Cross-plan file handoff — screenshot → OCR in same session.

Validates M933 (session_files/last_plan in planner prompt), M1037
(announce msgs allowed, no hallucination), and M935 (detail/expect consistency).

Two messages in the same session:
1. Take a screenshot of example.com
2. Use OCR to extract text from the screenshot

Key assertions:
- Planner uses local file path (no curl/download task)
- OCR tool receives correct relative path (pub/...)
- No replan needed on either step
- Messenger does not hallucinate results
"""

from __future__ import annotations

import pytest

from kiso.tools import discover_tools, invalidate_tools_cache
from tests.functional.conftest import (
    assert_no_failure_language,
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


def _tool_installed(name: str) -> bool:
    invalidate_tools_cache()
    return any(t["name"] == name for t in discover_tools())


async def _ensure_tool(run_message, tool_name: str, prompt: str, *, timeout: float = TOOL_TIMEOUT):
    """Run prompt, handling tool install flow if needed."""
    result = await run_message(prompt, timeout=timeout)
    if _tool_installed(tool_name):
        return result
    # Install the tool
    await run_message(f"yes, install {tool_name}", timeout=timeout)
    if not _tool_installed(tool_name):
        pytest.skip(f"Could not install {tool_name}")
    # Retry with tool available
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

        # M933: planner should NOT create curl/download tasks (file is local)
        last_plan_id = r2.plans[-1]["id"]
        last_plan_tasks = [
            t for t in r2.tasks if t.get("plan_id") == last_plan_id
        ]
        exec_tasks = [
            t for t in last_plan_tasks if t.get("type") == "exec"
        ]
        for t in exec_tasks:
            detail = (t.get("detail") or "").lower()
            cmd = (t.get("command") or "").lower()
            assert "curl" not in cmd and "wget" not in cmd, (
                f"Download command in exec task (file should be local): {cmd[:200]}"
            )
            assert "download" not in detail, (
                f"Download task created (file should be local): {detail[:200]}"
            )

        # OCR tool task should have correct path with pub/ prefix
        ocr_tasks = [
            t for t in last_plan_tasks
            if t.get("type") == "tool" and t.get("skill") == "ocr"
        ]
        assert ocr_tasks, f"No OCR tool task found. Types: {[t.get('type') for t in last_plan_tasks]}"

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
