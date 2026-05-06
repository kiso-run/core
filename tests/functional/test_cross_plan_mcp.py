"""Cross-plan MCP handoff — navigate then extract_text in two plans.

Replaces the F36 coverage that depended on the retired wrapper
subsystem. Uses the deterministic `requires_mcp` mock catalog
(tests/conftest.py:_CAPABILITY_HINTS): registering
`browser-mcp` auto-binds the `navigate` capability with a
realistic page-content callback, and registering `ocr-mcp` binds
the `extract_text` capability returning a mock invoice.

Two-message flow in the same session validates:
- The planner picks `browser-mcp` for the navigate intent
  (M1609 capability rule: prefer the installed MCP).
- The planner picks `ocr-mcp` for the extract-text intent in a
  separate plan, exercising cross-plan capability routing.
- The OCR mock content reaches the final messenger output (proves
  the second MCP was actually invoked, not skipped).
- No `exec` task re-implements navigate / OCR via curl or inline
  scripts.
"""

from __future__ import annotations

import pytest

from tests.conftest import LLM_MULTI_PLAN_TIMEOUT
from tests.functional.conftest import assert_no_command_word

pytestmark = pytest.mark.functional


@pytest.mark.usefixtures("clean_session")
class TestCrossPlanMCPHandoff:
    """Cross-plan MCP A → MCP B handoff: navigate → extract_text."""

    @pytest.mark.requires_mcp(["browser-mcp", "ocr-mcp"])
    async def test_navigate_then_extract_text_via_separate_mcps(
        self, run_message,
    ):
        """What: Plan 1 navigates to a URL via browser-mcp, plan 2
        extracts text from the result via ocr-mcp, in the same session.

        Why: Validates that the planner picks the right capability-
        flavoured MCP for each intent (M1609) and routes between two
        MCPs across separate plans without falling back to inline exec.

        Expects: a browser-mcp:navigate task in plan 1, an
        ocr-mcp:extract_text task in plan 2, and an "Acme" /
        "Invoice" / "INV-2025-0142" token from the OCR mock callback
        reaching the final messenger output.
        """
        # --- Plan 1: navigate to a URL via browser-mcp ---
        r1 = await run_message(
            "naviga a https://example.com e prendi il contenuto della pagina",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r1.success, (
            f"Plan 1 (navigate) failed. Plans: "
            f"{[p.get('status') for p in r1.plans]}"
        )
        navigate_calls = [
            t for t in r1.tasks
            if t.get("type") == "mcp"
            and t.get("server") == "browser-mcp"
        ]
        assert navigate_calls, (
            f"Plan 1 must use browser-mcp (capability: navigate). "
            f"Task types: {r1.task_types()}, "
            f"servers: {[t.get('server') for t in r1.tasks if t.get('type') == 'mcp']}"
        )
        # Defensive: the planner must not re-implement navigate via
        # inline curl / wget when an MCP exists for the intent (M1609).
        assert_no_command_word(r1.tasks, ["curl", "wget"])

        # --- Plan 2: extract text via ocr-mcp ---
        r2 = await run_message(
            "ora estrai il testo (OCR) dal contenuto della pagina precedente "
            "e dimmi cosa contiene",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r2.success, (
            f"Plan 2 (extract_text) failed. Plans: "
            f"{[p.get('status') for p in r2.plans]}"
        )
        last_plan_id = r2.plans[-1]["id"]
        ocr_calls = [
            t for t in r2.tasks
            if t.get("type") == "mcp"
            and t.get("server") == "ocr-mcp"
            and t.get("plan_id") == last_plan_id
        ]
        assert ocr_calls, (
            f"Plan 2 must use ocr-mcp (capability: extract_text). "
            f"Task types: {r2.task_types()}, "
            f"servers: {[t.get('server') for t in r2.tasks if t.get('type') == 'mcp']}"
        )
        assert_no_command_word(r2.tasks, ["curl", "wget", "tesseract"])

        # The OCR mock callback returns a fixed invoice fragment
        # ("Acme Corporation", "INV-2025-0142", "1,250.00 EUR").
        # At least one recognisable token must reach the messenger
        # output, proving the second MCP was actually invoked and its
        # result flowed into the user-visible reply.
        msg_output = r2.last_plan_msg_output.lower()
        assert any(
            tok in msg_output
            for tok in ("acme", "inv-2025", "1,250", "1.250", "invoice", "fattura")
        ), (
            f"Plan 2 messenger output does not contain OCR mock tokens "
            f"(Acme / INV-2025 / 1250). Output: "
            f"{r2.last_plan_msg_output[:400]}"
        )
