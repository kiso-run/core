"""M617: Integration test infrastructure.

Provides fixtures for connector protocol integration tests:
- ``integration`` marker gating
- ``kiso_app`` — ASGI app with mock LLM and isolated DB
- ``kiso_client`` — authenticated httpx AsyncClient
- ``webhook_collector`` — captures webhook deliveries
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from kiso.config import load_config
from kiso.main import app, _init_app_state
from kiso.store import init_db

# ---------------------------------------------------------------------------
# Integration marker gating (added to root conftest.py)
# ---------------------------------------------------------------------------
# NOTE: The --integration flag and marker skip logic are added in the root
# conftest.py for consistency with the existing gating pattern.


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INTEGRATION_CONFIG = """\
[tokens]
cli = "test-secret-token"
connector = "connector-token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[users.testadmin]
role = "admin"

[users.testuser]
role = "user"
tools = "*"

[users.testuser.aliases]
connector = "TestUser"

[models]
briefer     = "test-briefer"
classifier  = "test-classifier"
planner     = "test-planner"
reviewer    = "test-reviewer"
curator     = "test-curator"
worker      = "test-worker"
summarizer  = "test-summarizer"
paraphraser = "test-paraphraser"
messenger   = "test-messenger"
searcher    = "test-searcher"
dreamer     = "test-dreamer"

[settings]
context_messages          = 7
summarize_threshold       = 30
bot_name                  = "Kiso"
knowledge_max_facts       = 50
fact_decay_days           = 7
fact_decay_rate           = 0.1
fact_archive_threshold    = 0.3
fact_consolidation_min_ratio = 0.3
max_replan_depth          = 3
max_validation_retries    = 3
max_plan_tasks            = 20
llm_timeout              = 5
max_output_size           = 1048576
max_worker_retries        = 1
max_llm_calls_per_message = 200
max_message_size          = 65536
max_queue_size            = 50
host                      = "0.0.0.0"
port                      = 8333
worker_idle_timeout       = 1
fast_path_enabled         = false
briefer_enabled           = false
webhook_allow_list        = ["127.0.0.1", "::1"]
webhook_require_https     = false
webhook_secret            = ""
webhook_max_payload       = 1048576
"""

AUTH_HEADER = {"Authorization": "Bearer test-secret-token"}
CONNECTOR_AUTH_HEADER = {"Authorization": "Bearer connector-token"}


# ---------------------------------------------------------------------------
# Webhook collector
# ---------------------------------------------------------------------------

class WebhookCollector:
    """Collects webhook delivery calls for assertion."""

    def __init__(self):
        self.deliveries: list[dict] = []
        self._event = asyncio.Event()

    def record(self, url, session, task_id, content, final, **kwargs):
        self.deliveries.append({
            "url": url,
            "session": session,
            "task_id": task_id,
            "content": content,
            "final": final,
            **kwargs,
        })
        self._event.set()

    async def wait_for_delivery(self, timeout: float = 10.0) -> dict:
        """Wait for the next delivery. Raises TimeoutError if none arrives."""
        await asyncio.wait_for(self._event.wait(), timeout=timeout)
        self._event.clear()
        return self.deliveries[-1]

    def clear(self):
        self.deliveries.clear()
        self._event.clear()


# ---------------------------------------------------------------------------
# Mock LLM responses
# ---------------------------------------------------------------------------

def make_classifier_response(category: str = "chat", lang: str = "en") -> str:
    return f"{category}:{lang}"


def make_plan_response(detail: str = "Hello!") -> str:
    """Return a minimal valid plan JSON for a single msg task."""
    plan = {
        "goal": "Respond to user",
        "tasks": [
            {
                "type": "msg",
                "detail": f"Answer in English. {detail}",
                "expect": None,
            }
        ],
    }
    return json.dumps(plan)


def make_messenger_response(content: str = "Hello! How can I help?") -> str:
    return content


def make_briefing_response() -> str:
    return json.dumps({
        "modules": [],
        "skills": [],
        "context": [],
        "output_indices": [],
        "relevant_tags": [],
        "exclude_recipes": [], "relevant_entities": [],
    })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def integration_config_path(tmp_path: Path) -> Path:
    """Write integration config and return path."""
    p = tmp_path / "config.toml"
    p.write_text(INTEGRATION_CONFIG)
    return p


@pytest_asyncio.fixture()
async def integration_db(tmp_path: Path):
    """Fresh DB for integration tests."""
    conn = await init_db(tmp_path / "integration.db")
    yield conn
    await conn.close()


@pytest.fixture()
def webhook_collector():
    """Create a webhook collector and patch deliver_webhook."""
    return WebhookCollector()


@pytest_asyncio.fixture()
async def kiso_client(
    tmp_path: Path,
    integration_config_path: Path,
    integration_db,
    webhook_collector,
):
    """Authenticated httpx AsyncClient wired to the FastAPI app via ASGI.

    LLM calls are mocked. Webhook delivery is captured by the collector.
    The worker runs as a real asyncio task but with mocked LLM.
    """
    config = load_config(integration_config_path)
    _init_app_state(app, config, integration_db)

    # Create mock LLM that returns appropriate responses based on role
    call_count = {"n": 0}

    async def mock_call_llm(config_obj, role, messages, *, session=None,
                            response_format=None, model_override=None,
                            **kwargs):
        call_count["n"] += 1
        # Determine response from role
        if role == "classifier":
            return make_classifier_response("plan", "en")
        elif role == "planner":
            return make_plan_response("Hello from integration test!")
        elif role == "messenger":
            return make_messenger_response("Integration test response.")
        elif role == "reviewer":
            return json.dumps({
                "status": "ok",
                "reason": None,
                "learn": None,
                "retry_hint": None,
                "summary": "Task completed.",
            })
        elif role == "briefer":
            return make_briefing_response()
        elif role == "summarizer":
            return "Summary of conversation."
        elif role == "paraphraser":
            return "Paraphrased content."
        else:
            return "Generic response."

    # Mock webhook delivery to use collector
    async def mock_deliver_webhook(url, session, task_id, content, final, **kw):
        webhook_collector.record(url, session, task_id, content, final, **kw)
        return (True, 200, 1)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("kiso.brain.call_llm", side_effect=mock_call_llm),
            patch("kiso.worker.loop.deliver_webhook", side_effect=mock_deliver_webhook),
        ):
            yield client


async def wait_for_worker_idle(client: httpx.AsyncClient, session: str,
                                timeout: float = 10.0) -> dict:
    """Poll /status until worker is idle. Returns final status."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(
            f"/status/{session}",
            params={"user": "testadmin", "after": "0"},
            headers=AUTH_HEADER,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("worker_running"):
                return data
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Worker for session {session} did not become idle within {timeout}s")
