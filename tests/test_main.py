"""Tests for GET /sessions/{session}/info and /status endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import re

from kiso.main import _init_kiso_dirs
from kiso.store import (
    create_plan,
    create_session,
    create_task,
    update_plan_status,
    update_plan_usage,
    update_task,
    update_task_usage,
)
from tests.conftest import AUTH_HEADER


async def test_get_session_info(client: httpx.AsyncClient):
    """Endpoint returns message_count for a session with messages."""
    await client.post("/msg", json={
        "session": "info-sess",
        "user": "testuser",
        "content": "hello",
    }, headers=AUTH_HEADER)
    await client.post("/msg", json={
        "session": "info-sess",
        "user": "testuser",
        "content": "world",
    }, headers=AUTH_HEADER)

    resp = await client.get("/sessions/info-sess/info", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["session"] == "info-sess"
    assert data["message_count"] == 2
    assert data["summary"] is None


async def test_get_session_info_no_session(client: httpx.AsyncClient):
    """Non-existent session returns count 0 and no summary."""
    resp = await client.get("/sessions/nonexistent/info", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["session"] == "nonexistent"
    assert data["message_count"] == 0
    assert data["summary"] is None


# ── /status verbose mode ──────────────────────────────────────


async def _seed_status_data(client: httpx.AsyncClient) -> str:
    """Create a session with a plan, task, and llm_calls containing verbose data."""
    from kiso.main import app

    db = app.state.db
    session = "verbose-test"
    await create_session(db, session)

    plan_id = await create_plan(db, session, message_id=1, goal="Test goal")
    task_id = await create_task(db, plan_id, session, "msg", "respond")
    await update_task(db, task_id, "done", output="Hello!")

    llm_calls = [
        {
            "role": "planner",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [{"role": "user", "content": "test prompt"}],
            "response": '{"goal": "test"}',
        },
    ]
    await update_task_usage(db, task_id, 100, 50, llm_calls=llm_calls)
    await update_plan_status(db, plan_id, "done")
    await update_plan_usage(db, plan_id, 100, 50, model="gpt-4", llm_calls=llm_calls)

    return session


async def test_status_default_strips_verbose_fields(client: httpx.AsyncClient):
    """GET /status/{session} default strips messages/response from llm_calls."""
    session = await _seed_status_data(client)

    resp = await client.get(f"/status/{session}", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()

    # Check tasks
    for task in data["tasks"]:
        if task.get("llm_calls"):
            calls = json.loads(task["llm_calls"])
            for c in calls:
                assert "messages" not in c
                assert "response" not in c

    # Check plan
    plan = data["plan"]
    if plan and plan.get("llm_calls"):
        calls = json.loads(plan["llm_calls"])
        for c in calls:
            assert "messages" not in c
            assert "response" not in c


async def test_status_verbose_includes_verbose_fields(client: httpx.AsyncClient):
    """GET /status/{session}?verbose=true includes messages/response in llm_calls."""
    session = await _seed_status_data(client)

    resp = await client.get(
        f"/status/{session}", params={"verbose": "true"}, headers=AUTH_HEADER
    )
    assert resp.status_code == 200
    data = resp.json()

    # Check tasks
    found_verbose = False
    for task in data["tasks"]:
        if task.get("llm_calls"):
            calls = json.loads(task["llm_calls"])
            for c in calls:
                if "messages" in c:
                    found_verbose = True
                    assert c["messages"] == [{"role": "user", "content": "test prompt"}]
                    assert c["response"] == '{"goal": "test"}'
    assert found_verbose, "Expected verbose fields in at least one llm_calls entry"


# ── _init_kiso_dirs ──────────────────────────────────────────


class TestInitKisoDirs:
    def test_creates_sys_subdirectories(self, tmp_path):
        """_init_kiso_dirs creates sys/bin and sys/ssh directories."""
        with patch("kiso.main.KISO_DIR", tmp_path):
            _init_kiso_dirs()
        assert (tmp_path / "sys" / "bin").is_dir()
        assert (tmp_path / "sys" / "ssh").is_dir()

    def test_creates_reference_directory(self, tmp_path):
        """_init_kiso_dirs creates the reference directory."""
        with patch("kiso.main.KISO_DIR", tmp_path):
            _init_kiso_dirs()
        assert (tmp_path / "reference").is_dir()

    def test_syncs_reference_docs(self, tmp_path):
        """_init_kiso_dirs syncs bundled .md files to reference/."""
        with patch("kiso.main.KISO_DIR", tmp_path):
            _init_kiso_dirs()
        ref_dir = tmp_path / "reference"
        assert (ref_dir / "skills.md").is_file()
        assert (ref_dir / "connectors.md").is_file()
        # Verify content is non-empty
        assert len((ref_dir / "skills.md").read_text()) > 0
        assert len((ref_dir / "connectors.md").read_text()) > 0

    def test_only_writes_when_changed(self, tmp_path):
        """_init_kiso_dirs doesn't rewrite files that haven't changed."""
        with patch("kiso.main.KISO_DIR", tmp_path):
            _init_kiso_dirs()
        mtime1 = (tmp_path / "reference" / "skills.md").stat().st_mtime
        with patch("kiso.main.KISO_DIR", tmp_path):
            _init_kiso_dirs()
        mtime2 = (tmp_path / "reference" / "skills.md").stat().st_mtime
        assert mtime1 == mtime2

    def test_idempotent(self, tmp_path):
        """Calling _init_kiso_dirs twice doesn't fail."""
        with patch("kiso.main.KISO_DIR", tmp_path):
            _init_kiso_dirs()
            _init_kiso_dirs()
        assert (tmp_path / "sys" / "bin").is_dir()


# --- Dockerfile entrypoint consistency ---


def test_dockerfile_uvicorn_module_is_importable():
    """The uvicorn module:app string in Dockerfile CMD must be importable.

    Catches renames like server.py → main.py where the Dockerfile CMD is
    left pointing at the old name. No Docker needed — just string + import.
    """
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    assert dockerfile.exists(), "Dockerfile not found at repo root"

    text = dockerfile.read_text()
    # Match: "uvicorn", "module.path:attr"
    m = re.search(r'"uvicorn",\s*"([^"]+)"', text)
    assert m, "Could not find uvicorn module:app argument in Dockerfile CMD"

    module_ref = m.group(1)          # e.g. "kiso.main:app"
    assert ":" in module_ref, f"Expected module:attr format, got {module_ref!r}"
    module_path, attr = module_ref.split(":", 1)

    import importlib
    mod = importlib.import_module(module_path)
    assert hasattr(mod, attr), (
        f"Module '{module_path}' has no attribute '{attr}' — "
        f"Dockerfile CMD is pointing at the wrong module"
    )
