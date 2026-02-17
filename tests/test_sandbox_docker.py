"""Docker integration tests for per-session exec sandbox.

Requires root to create Linux users and chown directories.
Run inside the dev container:
    docker compose exec dev uv run pytest tests/test_sandbox_docker.py -v
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile

import pytest

from kiso.worker import _ensure_sandbox_user, _exec_task, _session_workspace

pytestmark = pytest.mark.skipif(
    os.getuid() != 0, reason="requires root",
)


@pytest.fixture()
def sandbox_session(tmp_path, monkeypatch):
    """Create a per-session sandbox user and locked workspace."""
    import kiso.worker

    monkeypatch.setattr(kiso.worker, "KISO_DIR", tmp_path)

    session = "integration-sandbox-test"
    uid = _ensure_sandbox_user(session)
    assert uid is not None, "useradd failed — are we running as root?"

    workspace = _session_workspace(session, sandbox_uid=uid)
    return session, uid, workspace


class TestSandboxIsolation:
    def test_workspace_owned_by_sandbox_user(self, sandbox_session):
        _, uid, workspace = sandbox_session
        stat = workspace.stat()
        assert stat.st_uid == uid
        assert oct(stat.st_mode & 0o777) == "0o700"

    def test_sandbox_user_can_write_inside_workspace(self, sandbox_session):
        session, uid, workspace = sandbox_session
        # Write a file as the sandbox user
        result = subprocess.run(
            ["su", "-s", "/bin/sh", "-c", f"echo hello > {workspace}/test.txt",
             f"kiso-s-{_session_hash(session)}"],
            capture_output=True,
        )
        assert result.returncode == 0
        assert (workspace / "test.txt").read_text().strip() == "hello"

    def test_sandbox_user_cannot_read_outside_workspace(self, sandbox_session):
        session, uid, workspace = sandbox_session
        # Create a file outside the workspace that only root can read
        outside = workspace.parent / "secret.txt"
        outside.write_text("top-secret")
        os.chown(outside, 0, 0)
        os.chmod(outside, 0o600)

        username = f"kiso-s-{_session_hash(session)}"
        result = subprocess.run(
            ["su", "-s", "/bin/sh", "-c", f"cat {outside}", username],
            capture_output=True,
        )
        assert result.returncode != 0  # permission denied

    async def test_exec_task_runs_as_sandbox_user(self, sandbox_session):
        session, uid, workspace = sandbox_session
        import kiso.worker

        stdout, stderr, success = await _exec_task(
            session, "id -u", 5, sandbox_uid=uid,
        )
        assert success is True
        assert stdout.strip() == str(uid)


def _session_hash(session: str) -> str:
    import hashlib
    return hashlib.sha256(session.encode()).hexdigest()[:12]
