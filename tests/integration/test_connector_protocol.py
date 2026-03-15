"""M618: Connector protocol integration tests.

Tests the full connector flow:
- Session registration via POST /sessions
- Message submission via POST /msg
- Worker processing with mock LLM
- Webhook delivery capture
- Polling fallback via GET /status
- Unknown user handling
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    AUTH_HEADER,
    CONNECTOR_AUTH_HEADER,
    wait_for_worker_idle,
)

pytestmark = pytest.mark.integration


class TestSessionRegistration:
    async def test_create_session_returns_201(self, kiso_client):
        resp = await kiso_client.post(
            "/sessions",
            json={"session": "int-test-1", "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["session"] == "int-test-1"
        assert data["created"] is True

    async def test_reregister_session_returns_200(self, kiso_client):
        await kiso_client.post(
            "/sessions",
            json={"session": "int-test-2"},
            headers=AUTH_HEADER,
        )
        resp = await kiso_client.post(
            "/sessions",
            json={"session": "int-test-2", "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["created"] is False


class TestMessageWebhookDelivery:
    async def test_msg_triggers_webhook(self, kiso_client, webhook_collector):
        """POST /msg → worker processes → webhook delivered."""
        # Register session with webhook
        await kiso_client.post(
            "/sessions",
            json={"session": "wh-test-1", "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )

        # Send message
        resp = await kiso_client.post(
            "/msg",
            json={"session": "wh-test-1", "user": "testadmin", "content": "hello"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["queued"] is True

        # Wait for worker to finish
        await wait_for_worker_idle(kiso_client, "wh-test-1")

        # Verify webhook was delivered
        assert len(webhook_collector.deliveries) > 0
        last = webhook_collector.deliveries[-1]
        assert last["session"] == "wh-test-1"
        assert last["final"] is True
        assert len(last["content"]) > 0


class TestPollingFallback:
    async def test_status_shows_tasks_after_processing(self, kiso_client):
        """Without webhook, client can poll /status for results."""
        # Create session without webhook
        await kiso_client.post(
            "/sessions",
            json={"session": "poll-test-1"},
            headers=AUTH_HEADER,
        )

        # Send message
        await kiso_client.post(
            "/msg",
            json={"session": "poll-test-1", "user": "testadmin", "content": "hi"},
            headers=AUTH_HEADER,
        )

        # Wait and poll
        status = await wait_for_worker_idle(kiso_client, "poll-test-1")
        assert status["worker_running"] is False
        # Should have tasks from the plan
        assert len(status["tasks"]) > 0


class TestUnknownUser:
    async def test_unknown_user_returns_untrusted(self, kiso_client):
        """POST /msg with unknown user → untrusted response."""
        await kiso_client.post(
            "/sessions",
            json={"session": "unknown-test-1"},
            headers=AUTH_HEADER,
        )
        resp = await kiso_client.post(
            "/msg",
            json={"session": "unknown-test-1", "user": "nobody", "content": "hi"},
            headers=AUTH_HEADER,
        )
        data = resp.json()
        assert data.get("untrusted") is True
