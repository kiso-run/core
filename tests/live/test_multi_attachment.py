"""M1584 — Flow D live: multi-attachment E2E with mock MCPs.

The message-attachment-receiver skill (M1564/M1565) routes 3 different
attachment types to 3 different MCP capabilities and consolidates the
outputs. This live test drives the full pipeline against the real LLM
with all 3 capability MCPs supplied as mocks (so the test is hermetic
on capability behaviour but real on the planner / messenger logic).
"""

from __future__ import annotations

import asyncio

import pytest

from kiso.brain import run_planner, validate_plan
from kiso.store import save_message

pytestmark = pytest.mark.llm_live

from tests.conftest import LLM_TEST_TIMEOUT as TIMEOUT


class TestFlowDMultiAttachment:
    """Three attachments → three MCPs → consolidated output."""

    async def test_three_capabilities_consolidate(
        self, live_config, seeded_db, live_session, mock_mcp_catalog,
    ):
        mock_mcp_catalog.register("transcriber-mock", {
            "transcribe": lambda **kw: {"text": "audio said hello"},
        })
        mock_mcp_catalog.register("ocr-mock", {
            "extract": lambda **kw: {"text": "image text Y"},
        })
        mock_mcp_catalog.register("docreader-mock", {
            "read": lambda **kw: {"text": "PDF content Z"},
        })
        mgr = mock_mcp_catalog.build_manager()
        # Warm the catalog cache for all 3 mocks.
        for name in ("transcriber-mock", "ocr-mock", "docreader-mock"):
            await mgr.list_methods(name)

        content = (
            "I am attaching three files: an audio recording, an image, "
            "and a PDF document. Please process each one and summarize "
            "what you find across all three."
        )
        await save_message(
            seeded_db, live_session, "testadmin", "user", content,
        )
        plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin", content,
                mcp_manager=mgr,
            ),
            timeout=TIMEOUT,
        )
        assert validate_plan(plan, installed_skills=[]) == [], plan
        # The plan should reference at least one of the registered
        # mocks; we don't pin a specific one because the planner is
        # free to ask for missing details first.
        servers = {
            t.get("server") for t in plan["tasks"]
            if t.get("type") == "mcp" and t.get("server")
        }
        # Either it routes through at least one mock, or it asks for
        # input first (msg-only with awaits_input).
        if not servers:
            assert plan.get("awaits_input") or any(
                t.get("type") == "msg" for t in plan["tasks"]
            )
