"""Integration test infrastructure.

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
consolidator = "test-consolidator"

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
# Webhook collector / dropper
# ---------------------------------------------------------------------------

class WebhookCollector:
    """Collects webhook delivery calls for assertion.

    Supports configurable failure modes for retry/polling-fallback testing:

    - ``failure_mode="ok"`` (default): every call records and returns
      ``(True, 200, attempts=1)``.
    - ``failure_mode="drop_n"`` with ``drop_count=N``: the first N
      attempts return ``(False, 500, attempts=k)`` and DO NOT record;
      attempt N+1 records and returns ``(True, 200, attempts=N+1)``.
    - ``failure_mode="retry_then_ok"`` with ``simulated_attempts=N``:
      simulates that ``deliver_webhook`` retried internally N times
      before succeeding. Returns ``(True, 200, N)`` on every call and
      records the delivery with the simulated attempts count.
    - ``failure_mode="always_500"``: every attempt returns
      ``(False, 500, attempts=k)`` and never records.
    - ``failure_mode="always_drop"``: connector-style "delivery silently
      lost" — never records, returns ``(False, 0, 0)``.

    The ``attempts_log`` list captures every call (record or not) so
    tests can assert exact attempt counts.
    """

    def __init__(self):
        self.deliveries: list[dict] = []
        self.attempts_log: list[dict] = []
        self._event = asyncio.Event()
        self.failure_mode: str = "ok"
        self.drop_count: int = 0
        self.simulated_attempts: int = 1
        self._call_index = 0

    def configure(self, *, failure_mode: str = "ok", drop_count: int = 0,
                  simulated_attempts: int = 1):
        """Configure the failure mode for subsequent calls."""
        self.failure_mode = failure_mode
        self.drop_count = drop_count
        self.simulated_attempts = simulated_attempts
        self._call_index = 0

    def deliver(self, url, session, task_id, content, final, **kwargs):
        """Simulate a webhook delivery. Returns (success, status_code, attempts)."""
        self._call_index += 1
        attempt_record = {
            "url": url,
            "session": session,
            "task_id": task_id,
            "call_index": self._call_index,
        }
        self.attempts_log.append(attempt_record)

        if self.failure_mode == "ok":
            self._record_delivery(url, session, task_id, content, final, **kwargs)
            return (True, 200, 1)

        if self.failure_mode == "drop_n":
            if self._call_index <= self.drop_count:
                return (False, 500, self._call_index)
            self._record_delivery(url, session, task_id, content, final, **kwargs)
            return (True, 200, self._call_index)

        if self.failure_mode == "retry_then_ok":
            self._record_delivery(url, session, task_id, content, final, **kwargs)
            self.deliveries[-1]["simulated_attempts"] = self.simulated_attempts
            return (True, 200, self.simulated_attempts)

        if self.failure_mode == "always_500":
            return (False, 500, self._call_index)

        if self.failure_mode == "always_drop":
            return (False, 0, 0)

        raise ValueError(f"Unknown failure_mode: {self.failure_mode}")

    def record(self, url, session, task_id, content, final, **kwargs):
        """Backward-compat shim for tests using the old record() API."""
        self._record_delivery(url, session, task_id, content, final, **kwargs)

    def _record_delivery(self, url, session, task_id, content, final, **kwargs):
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
        """Wait for the next successful delivery."""
        await asyncio.wait_for(self._event.wait(), timeout=timeout)
        self._event.clear()
        return self.deliveries[-1]

    def clear(self):
        self.deliveries.clear()
        self.attempts_log.clear()
        self._event.clear()
        self._call_index = 0


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

    # Mock webhook delivery to use collector. The collector decides whether
    # to record + succeed or simulate failure based on its configured
    # failure_mode (set via webhook_collector.configure(...) in the test).
    async def mock_deliver_webhook(url, session, task_id, content, final, **kw):
        return webhook_collector.deliver(url, session, task_id, content, final, **kw)

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


# ---------------------------------------------------------------------------
# Fake tool package
# ---------------------------------------------------------------------------

# Minimal manifest schema accepted by Kiso. The script reads its stdin
# (a JSON payload from the worker), echoes the keys it received plus
# the env vars it can see and any session_secrets keys, and exits 0.
# Designed for M1273 secret-containment tests where the test asserts
# what the tool actually receives vs what was declared.

_FAKE_TOOL_MANIFEST = """\
[kiso.tool]
name = "{name}"
description = "Fake tool for integration tests — echoes stdin keys and env"
version = "0.0.1"
session_secrets = ["DECLARED_KEY"]
"""

_FAKE_TOOL_SCRIPT = '''\
#!/usr/bin/env python3
"""Echoes received stdin keys, declared session_secrets, and visible env."""
import json
import os
import sys


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw}

    report = {
        "stdin_keys": sorted(payload.keys()),
        "session_secrets_keys": sorted((payload.get("session_secrets") or {}).keys()),
        "session_secrets_values": payload.get("session_secrets") or {},
        "env_keys_visible": sorted([k for k in os.environ.keys() if not k.startswith("_")]),
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()
'''


@pytest.fixture()
def fake_tool(tmp_path: Path):
    """Create a fake Kiso tool package in a temp directory.

    Returns a dict shaped like the entries produced by
    ``kiso.wrappers.discover_wrappers``: ``{"name", "path", "session_secrets",
    "env", ...}``. The tool's `run.py` reads stdin as JSON and prints
    a containment report. Used by M1273 secret containment tests to
    assert via real subprocess execution.
    """
    name = "faketool"
    tool_dir = tmp_path / "wrappers" / name
    tool_dir.mkdir(parents=True)
    (tool_dir / "manifest.toml").write_text(_FAKE_TOOL_MANIFEST.format(name=name))
    run_py = tool_dir / "run.py"
    run_py.write_text(_FAKE_TOOL_SCRIPT)
    run_py.chmod(0o755)
    return {
        "name": name,
        "path": str(tool_dir),
        "session_secrets": ["DECLARED_KEY"],
        "env": {},
        "summary": "fake",
        "args_schema": {},
        "version": "0.0.1",
        "description": "fake",
    }


# ---------------------------------------------------------------------------
# Fake connector directory
# ---------------------------------------------------------------------------

# Minimal connector launcher. Behavior is controlled by env vars set by
# the test fixture, so the same launcher can simulate clean exit, crash,
# hang, or stable run depending on what the supervisor lifecycle test
# needs to verify.

_FAKE_CONNECTOR_LAUNCHER = '''\
#!/usr/bin/env python3
"""Fake connector launcher for supervisor lifecycle tests.

Behavior controlled by env vars:
- FAKE_CONNECTOR_MODE=clean_exit  → exit 0 immediately
- FAKE_CONNECTOR_MODE=crash       → exit 1 immediately
- FAKE_CONNECTOR_MODE=hang        → sleep forever
- FAKE_CONNECTOR_MODE=stable      → sleep for FAKE_CONNECTOR_STABLE_SECS then exit 0
- FAKE_CONNECTOR_MODE=crash_after → run for FAKE_CONNECTOR_RUN_SECS, then exit 1
"""
import os
import sys
import time

mode = os.environ.get("FAKE_CONNECTOR_MODE", "stable")
if mode == "clean_exit":
    sys.exit(0)
if mode == "crash":
    sys.exit(1)
if mode == "hang":
    while True:
        time.sleep(60)
if mode == "stable":
    secs = float(os.environ.get("FAKE_CONNECTOR_STABLE_SECS", "0.5"))
    time.sleep(secs)
    sys.exit(0)
if mode == "crash_after":
    secs = float(os.environ.get("FAKE_CONNECTOR_RUN_SECS", "0.5"))
    time.sleep(secs)
    sys.exit(1)
sys.exit(2)
'''


@pytest.fixture()
def fake_connector_dir(tmp_path: Path):
    """Create a fake connector directory with a controllable launcher.

    Returns the path to the connector directory. The launcher reads
    `FAKE_CONNECTOR_MODE` from the environment so the test can pick:
    `clean_exit`, `crash`, `hang`, `stable`, or `crash_after`.

    Used by M1276 connector lifecycle tests to drive the supervisor
    deterministically without depending on a real connector binary.
    """
    name = "fakeconnector"
    conn_dir = tmp_path / "connectors" / name
    conn_dir.mkdir(parents=True)
    launcher = conn_dir / "launcher.py"
    launcher.write_text(_FAKE_CONNECTOR_LAUNCHER)
    launcher.chmod(0o755)
    # Minimal manifest so install/health checks have something to read
    (conn_dir / "manifest.toml").write_text(
        f'name = "{name}"\n'
        'description = "Fake connector for integration tests"\n'
        'version = "0.0.1"\n'
        'runtime = "python"\n'
        'entrypoint = "launcher.py"\n'
    )
    return conn_dir
