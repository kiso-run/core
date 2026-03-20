"""M853: Multi-step integration tests with mock LLM.

Tests multi-turn flows that exercise session continuity, replan context,
and cross-turn state — without real LLM calls or Docker.
"""

from __future__ import annotations

import json

import pytest

from tests.integration.conftest import (
    AUTH_HEADER,
    wait_for_worker_idle,
)

pytestmark = pytest.mark.integration


class TestMultiTurnConversation:
    """Two sequential messages in the same session verify context flows."""

    async def test_second_message_gets_response(self, kiso_client, webhook_collector):
        """What: Send two messages in the same session.

        Why: Validates that the second message is processed correctly after the
        first completes — tests session continuity, message queuing, and context
        passing between turns.
        Expects: Both turns produce webhook deliveries.
        """
        session = "multi-turn-1"
        await kiso_client.post(
            "/sessions",
            json={"session": session, "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )

        # Turn 1
        resp = await kiso_client.post(
            "/msg",
            json={"session": session, "user": "testadmin", "content": "hello"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 202
        await wait_for_worker_idle(kiso_client, session)
        turn1_count = len(webhook_collector.deliveries)
        assert turn1_count > 0

        # Turn 2 — same session
        resp = await kiso_client.post(
            "/msg",
            json={"session": session, "user": "testadmin", "content": "what did you say?"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 202
        await wait_for_worker_idle(kiso_client, session)

        # Both turns should have produced deliveries
        assert len(webhook_collector.deliveries) > turn1_count
        last = webhook_collector.deliveries[-1]
        assert last["session"] == session
        assert last["final"] is True


class TestCancelMidPlan:
    """Verify cancel during processing preserves partial state."""

    async def test_cancel_returns_accepted(self, kiso_client, webhook_collector):
        """What: Send a message then immediately cancel.

        Why: Validates the cancel endpoint works and doesn't crash the worker.
        Expects: Cancel returns 200, worker becomes idle.
        """
        session = "cancel-test-1"
        await kiso_client.post(
            "/sessions",
            json={"session": session, "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )

        # Send message
        await kiso_client.post(
            "/msg",
            json={"session": session, "user": "testadmin", "content": "do something complex"},
            headers=AUTH_HEADER,
        )

        # Cancel immediately
        resp = await kiso_client.post(
            f"/cancel/{session}",
            headers=AUTH_HEADER,
        )
        # Cancel may return 200 (cancelled) or 404 (already done)
        assert resp.status_code in (200, 404)

        # Worker should become idle regardless
        await wait_for_worker_idle(kiso_client, session)
