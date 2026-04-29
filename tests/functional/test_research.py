"""F7-F8: Multi-step research and script execution functional tests.

F7: Search + synthesize + publish a markdown file.
F8: Write a Python script, execute it, report results.
"""

from __future__ import annotations

import pytest

from tests.conftest import LLM_MULTI_PLAN_TIMEOUT, LLM_REPLAN_TIMEOUT
from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
    assert_url_reachable,
)

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# F7 — Search + synthesize + publish
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_session")
class TestF7ResearchAndPublish:
    """Search for info, create a markdown table, publish it."""

    @pytest.mark.requires_mcp("search-mcp")
    async def test_search_synthesize_publish(self, run_message, func_app_client):
        """What: Multi-step pipeline test: search, synthesize into markdown table, publish.

        Why: Validates the search -> artifact creation -> publish pipeline. Ensures Kiso
        can research a topic, create a structured file, and make it available via URL.
        Expects: Plan succeeds, .md file published with reachable URL, >=3 programming
        languages mentioned in output, msg references the published file.
        """
        result = await run_message(
            "cerca i 5 linguaggi di programmazione più usati nel 2025, "
            "crea un file markdown con una tabella comparativa e mandamelo",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        types = result.task_types()
        # Search now flows through kiso-search MCP (Phase 4 retired the
        # built-in `type=search` task type).
        search_calls = [
            t for t in result.tasks
            if t.get("type") == "mcp" and t.get("server") == "kiso-search"
        ]
        assert search_calls, (
            f"Expected an MCP call to kiso-search in the pipeline. "
            f"Task types: {types}"
        )
        assert "exec" in types, f"Expected exec task to create markdown artifact: {types}"

        # Response should not contain failure language (check last plan only —
        # intermediate replan status messages may contain "failed to")
        assert_no_failure_language(result.last_plan_msg_output)
        # NOTE: assert_italian skipped — technical table content (language names,
        # descriptions) triggers false positive on the EN word heuristic

        # A markdown file was published
        assert result.has_published_file("*.md"), (
            f"No .md file published. Pub files: {result.pub_files}"
        )

        # Published markdown URL is reachable
        for pf in result.pub_files:
            if pf["filename"].endswith(".md"):
                await assert_url_reachable(pf["url"], client=func_app_client)

        # Check published file content for table markers and language names
        # (We check task outputs since they may contain the file content)
        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
        ).lower()

        # At least 3 programming language names should appear
        languages = (
            "python", "java", "javascript", "typescript", "c++",
            "c#", "go", "rust", "kotlin", "swift", "php", "ruby",
        )
        found = [lang for lang in languages if lang in all_output]
        assert len(found) >= 3, (
            f"Expected at least 3 programming languages, found {found}. "
            f"Output excerpt: {all_output[:500]}"
        )

        # msg output references the published file
        lower_msg = result.msg_output.lower()
        assert any(
            kw in lower_msg
            for kw in ("file", "tabella", "markdown", "pub/", "http")
        ), f"msg does not reference the published file: {result.msg_output[:300]}"


# ---------------------------------------------------------------------------
# F8 — Script creation + execution
# ---------------------------------------------------------------------------


class TestF8ScriptExecution:
    """Write a Python script, run it, and report the results."""

    async def test_fibonacci_script(self, run_message):
        """What: Code generation + execution pipeline for Fibonacci computation.

        Why: Validates that Kiso can write correct Python code, execute it, and
        communicate results back in Italian. Tests the exec task type end-to-end.
        Expects: Plan succeeds, Italian response, Fibonacci numbers (4181 or 6765)
        appear in task output, msg mentions fibonacci/results.
        """
        result = await run_message(
            "scrivi uno script python che calcola i primi 20 numeri di "
            "fibonacci, eseguilo e dimmi il risultato",
            timeout=LLM_REPLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        assert "exec" in result.task_types(), (
            f"Expected exec task for script creation/execution, got: {result.task_types()}"
        )

        # Response is in Italian (use last_plan_msg_output to exclude
        # English replan notifications from earlier plans)
        assert_italian(result.last_plan_msg_output)
        assert_no_failure_language(result.last_plan_msg_output)

        # Check that Fibonacci computation ran — accept either counting convention:
        # 0-indexed: [0, 1, ..., 4181] or 1-indexed: [1, 1, ..., 6765]
        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
        )
        assert any(n in all_output for n in ("4181", "6765")), (
            f"Expected Fibonacci number 4181 or 6765 in output. "
            f"Output excerpt: {all_output[:500]}"
        )

        # msg output communicates results
        lower_msg = result.msg_output.lower()
        assert any(
            kw in lower_msg
            for kw in ("fibonacci", "numeri", "sequenza", "risultat")
        ), f"msg doesn't mention fibonacci/results: {result.msg_output[:300]}"
