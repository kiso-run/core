"""F7-F8: Multi-step research and script execution functional tests.

F7: Search + synthesize + publish a markdown file.
F8: Write a Python script, execute it, report results.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
    assert_url_reachable,
)

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# F7 — Search + synthesize + publish
# ---------------------------------------------------------------------------


class TestF7ResearchAndPublish:
    """Search for info, create a markdown table, publish it."""

    async def test_search_synthesize_publish(self, run_message):
        result = await run_message(
            "cerca i 5 linguaggi di programmazione più usati nel 2025, "
            "crea un file markdown con una tabella comparativa e mandamelo",
            timeout=300,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

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
                await assert_url_reachable(pf["url"])

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
        result = await run_message(
            "scrivi uno script python che calcola i primi 20 numeri di "
            "fibonacci, eseguilo e dimmi il risultato",
            timeout=180,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian
        assert_italian(result.msg_output)
        assert_no_failure_language(result.msg_output)

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
