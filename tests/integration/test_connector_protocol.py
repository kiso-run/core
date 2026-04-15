"""Connector protocol integration tests.

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


class TestWebhookRetryRouteLevel:
    """: prove the route → deliver_webhook → final-state pipeline
    propagates retry results correctly. Unit-level retry/backoff is
    already covered in tests/test_webhook.py:209-341 — this is the
    integration counterpart that asserts the runtime invokes the
    delivery path and surfaces the attempts count to the recorded
    delivery state."""

    async def test_msg_flow_records_simulated_internal_retries(
        self, kiso_client, webhook_collector,
    ):
        """A /msg flow whose deliver_webhook needed N internal retries
        before succeeding is recorded with attempts == N at the final
        delivery state."""
        webhook_collector.configure(failure_mode="retry_then_ok", simulated_attempts=3)

        await kiso_client.post(
            "/sessions",
            json={"session": "wh-retry-1", "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )
        resp = await kiso_client.post(
            "/msg",
            json={"session": "wh-retry-1", "user": "testadmin", "content": "retry me"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 202

        await wait_for_worker_idle(kiso_client, "wh-retry-1")

        finals = [d for d in webhook_collector.deliveries
                  if d["session"] == "wh-retry-1" and d["final"]]
        assert len(finals) >= 1
        assert all(d.get("simulated_attempts") == 3 for d in finals)


class TestPollingFallbackRecovery:
    """: prove that when webhook delivery is silently dropped, the
    connector can recover the missed response via GET /status?after=
    cursor."""

    async def test_dropped_webhook_recoverable_via_status_after_cursor(
        self, kiso_client, webhook_collector,
    ):
        """Configure the webhook to silently drop, post a message,
        drain, and assert that GET /status returns a task whose content
        is what would have been delivered. Then assert that re-polling
        with after=last_task_id returns no new tasks."""
        webhook_collector.configure(failure_mode="always_drop")

        await kiso_client.post(
            "/sessions",
            json={"session": "poll-fallback-1", "webhook": "http://localhost:9999/hook"},
            headers=AUTH_HEADER,
        )
        await kiso_client.post(
            "/msg",
            json={"session": "poll-fallback-1", "user": "testadmin", "content": "hi"},
            headers=AUTH_HEADER,
        )

        status = await wait_for_worker_idle(kiso_client, "poll-fallback-1")

        # No webhook deliveries were recorded — the dropper swallowed them
        assert webhook_collector.deliveries == []

        # The connector recovers the missed response via /status
        assert len(status["tasks"]) > 0
        last_task_id = max(t["id"] for t in status["tasks"])

        # Re-poll with after=last_task_id — no new tasks
        resp = await kiso_client.get(
            "/status/poll-fallback-1",
            params={"user": "testadmin", "after": last_task_id},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tasks"] == [] or all(
            t["id"] > last_task_id for t in data["tasks"]
        )


class TestStatusAfterCursor:
    """: explicit cursor correctness — only tasks past the cursor
    are returned, not the full historical status payload."""

    async def test_after_cursor_filters_old_tasks(self, kiso_client):
        await kiso_client.post(
            "/sessions",
            json={"session": "after-cursor-1"},
            headers=AUTH_HEADER,
        )
        await kiso_client.post(
            "/msg",
            json={"session": "after-cursor-1", "user": "testadmin", "content": "first"},
            headers=AUTH_HEADER,
        )
        status = await wait_for_worker_idle(kiso_client, "after-cursor-1")
        assert len(status["tasks"]) > 0
        all_task_ids = [t["id"] for t in status["tasks"]]
        max_id = max(all_task_ids)

        # Polling with after=0 returns everything
        resp_all = await kiso_client.get(
            "/status/after-cursor-1",
            params={"user": "testadmin", "after": 0},
            headers=AUTH_HEADER,
        )
        assert resp_all.status_code == 200
        assert len(resp_all.json()["tasks"]) == len(all_task_ids)

        # Polling with after=max_id returns nothing newer
        resp_after = await kiso_client.get(
            "/status/after-cursor-1",
            params={"user": "testadmin", "after": max_id},
            headers=AUTH_HEADER,
        )
        assert resp_after.status_code == 200
        new_tasks = resp_after.json()["tasks"]
        assert all(t["id"] > max_id for t in new_tasks)


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
