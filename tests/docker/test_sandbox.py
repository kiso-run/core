"""Docker integration tests for per-session exec sandbox.

Requires root to create Linux users and chown directories.
Run inside the dev container:
    docker compose -f docker-compose.test.yml --profile docker run --rm test-docker
"""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest

from kiso.worker.exec import _exec_task
from kiso.worker.utils import _ensure_sandbox_user_sync, _session_workspace

pytestmark = pytest.mark.skipif(
    os.getuid() != 0, reason="requires root",
)


def _session_hash(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()[:12]


@pytest.fixture()
def sandbox_session(kiso_dir):
    """Create a per-session sandbox user and locked workspace."""
    session = "integration-sandbox-test"
    uid = _ensure_sandbox_user_sync(session)
    assert uid is not None, "useradd failed — are we running as root?"

    workspace = _session_workspace(session, sandbox_uid=uid)
    return session, uid, workspace


class TestSandboxIsolation:
    def test_workspace_owned_by_sandbox_user(self, sandbox_session):
        """What: Checks the sandbox workspace directory ownership and permissions.

        Why: Validates the workspace is owned by the sandbox user with 0700 permissions (no group/other access).
        Expects: stat.st_uid matches sandbox uid, mode is 0o700.
        """
        _, uid, workspace = sandbox_session
        stat = workspace.stat()
        assert stat.st_uid == uid
        assert oct(stat.st_mode & 0o777) == "0o700"

    def test_sandbox_user_can_write_inside_workspace(self, sandbox_session):
        """What: The sandbox user writes a file inside the workspace via 'su'.

        Why: Validates the sandbox user has write access within their own workspace.
        Expects: File created successfully (exit 0), content matches 'hello'.
        """
        session, uid, workspace = sandbox_session
        username = f"kiso-s-{_session_hash(session)}"
        result = subprocess.run(
            ["su", "-s", "/bin/sh", "-c", f"echo hello > {workspace}/test.txt", username],
            capture_output=True,
        )
        assert result.returncode == 0
        assert (workspace / "test.txt").read_text().strip() == "hello"

    def test_sandbox_user_cannot_read_outside_workspace(self, sandbox_session):
        """What: Creates a root-owned file outside the workspace and tries to read it as the sandbox user.

        Why: Validates sandbox isolation — the user cannot access files outside their workspace.
        Expects: Non-zero exit code (permission denied).
        """
        session, uid, workspace = sandbox_session
        outside = workspace.parent / "secret.txt"
        outside.write_text("top-secret")
        os.chown(outside, 0, 0)
        os.chmod(outside, 0o600)

        username = f"kiso-s-{_session_hash(session)}"
        result = subprocess.run(
            ["su", "-s", "/bin/sh", "-c", f"cat {outside}", username],
            capture_output=True,
        )
        assert result.returncode != 0

    async def test_exec_task_runs_as_sandbox_user(self, sandbox_session):
        """What: Runs 'id -u' via _exec_task with sandbox_uid and checks the reported UID.

        Why: Validates that _exec_task actually executes commands as the sandbox user, not root.
        Expects: Command succeeds, stdout matches the sandbox UID.
        """
        session, uid, workspace = sandbox_session
        stdout, stderr, success, _ = await _exec_task(
            session, "id -u", sandbox_uid=uid,
        )
        assert success is True
        assert stdout.strip() == str(uid)
