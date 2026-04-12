"""Integration test — multi-turn approval flow.

Tests connected flows that span multiple plan cycles:
- Install proposal → user approval → exec install
- Session isolation (approval in one session doesn't leak to another)
"""

from __future__ import annotations

import json

import pytest

from tests.integration.conftest import (
    AUTH_HEADER,
    wait_for_worker_idle,
)

pytestmark = pytest.mark.integration


class TestMultiTurnApproval:
    async def test_install_proposal_then_approval(self, kiso_client, webhook_collector):
        """First message gets a proposal msg, second message triggers install."""
        session = "approval-test-1"

        # Register session with webhook
        await kiso_client.post(
            "/sessions",
            json={"session": session, "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )

        # First message: "install the browser wrapper"
        resp = await kiso_client.post(
            "/msg",
            json={"session": session, "user": "testadmin", "content": "install the browser wrapper"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 202

        # Wait for first turn to complete
        status = await wait_for_worker_idle(kiso_client, session)
        assert not status["worker_running"]

        # Should have received a webhook with the response
        assert len(webhook_collector.deliveries) > 0
        first_response = webhook_collector.deliveries[-1]
        assert first_response["session"] == session
        assert first_response["final"] is True

        # Second message: "yes, install it"
        webhook_collector.clear()
        resp = await kiso_client.post(
            "/msg",
            json={"session": session, "user": "testadmin", "content": "yes, install it"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 202

        # Wait for second turn
        status = await wait_for_worker_idle(kiso_client, session)

        # Should have received another webhook
        assert len(webhook_collector.deliveries) > 0
        second_response = webhook_collector.deliveries[-1]
        assert second_response["session"] == session


class TestSessionIsolation:
    async def test_approval_does_not_leak_between_sessions(self, kiso_client, webhook_collector):
        """Approval in session A does not affect session B."""
        # Register two sessions
        for sess in ("iso-A", "iso-B"):
            await kiso_client.post(
                "/sessions",
                json={"session": sess, "webhook": "http://localhost:9999/hook"},
                headers=AUTH_HEADER,
            )

        # Send message to session A
        await kiso_client.post(
            "/msg",
            json={"session": "iso-A", "user": "testadmin", "content": "install browser"},
            headers=AUTH_HEADER,
        )
        await wait_for_worker_idle(kiso_client, "iso-A")

        # Send message to session B — should be independent
        webhook_collector.clear()
        await kiso_client.post(
            "/msg",
            json={"session": "iso-B", "user": "testadmin", "content": "hello"},
            headers=AUTH_HEADER,
        )
        await wait_for_worker_idle(kiso_client, "iso-B")

        # Verify session B got its own response
        b_deliveries = [d for d in webhook_collector.deliveries if d["session"] == "iso-B"]
        assert len(b_deliveries) > 0
