"""`uploads/` attachment handoff end-to-end (M1272).

Unit-level plumbing for ``_session_workspace`` is already covered in
``tests/test_worker.py`` (``test_workspace_creates_uploads_dir`` and
neighbors). This module covers the **integration contract**: a file
that a connector writes into ``sessions/{session}/uploads/`` is
visible to the worker, persists across a `/msg` flow, and remains in
place after processing finishes.

The integration tests intentionally do **not** assert that the
planner *interprets* the file — that depends on real LLM behavior and
belongs to a live/functional test. Here we pin the workspace plumbing
contract so connectors and future file-driven flows can rely on it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from kiso.main import _workers, _worker_phases
from kiso.worker.utils import _session_workspace

from tests.integration.conftest import (
    AUTH_HEADER,
    wait_for_worker_idle,
)


pytestmark = pytest.mark.integration


async def _cleanup_workers(*sessions: str):
    for sess in sessions:
        entry = _workers.pop(sess, None)
        _worker_phases.pop(sess, None)
        if entry is not None and not entry.task.done():
            entry.task.cancel()


class TestUploadsWorkspaceHandoff:

    async def test_uploads_dir_created_at_expected_path(self, tmp_path: Path):
        """`_session_workspace` creates `uploads/` under the session
        workspace at `KISO_DIR/sessions/{session}/uploads/`."""
        with patch("kiso.worker.utils.KISO_DIR", tmp_path):
            ws = _session_workspace("upload-plumbing-1")
            uploads = ws / "uploads"
            assert uploads.is_dir()
            assert uploads.parent == tmp_path / "sessions" / "upload-plumbing-1"

    async def test_uploaded_file_visible_to_session_workspace(self, tmp_path: Path):
        """A file written to `uploads/` by a connector is readable from
        the workspace path the worker uses."""
        with patch("kiso.worker.utils.KISO_DIR", tmp_path):
            ws = _session_workspace("upload-plumbing-2")
            uploads = ws / "uploads"
            file_path = uploads / "report.txt"
            file_path.write_text("hello from connector")

            # Re-resolving the workspace returns the same path with the
            # uploaded file intact (idempotency of _session_workspace).
            ws_again = _session_workspace("upload-plumbing-2")
            assert (ws_again / "uploads" / "report.txt").read_text() == "hello from connector"

    async def test_uploaded_file_persists_across_msg_flow(
        self, kiso_client: httpx.AsyncClient, tmp_path: Path,
    ):
        """A file in `uploads/` is still present after a `/msg` flow
        completes — the worker does not delete or overwrite it."""
        sess = "upload-persist"
        with patch("kiso.worker.utils.KISO_DIR", tmp_path):
            ws = _session_workspace(sess)
            file_path = ws / "uploads" / "input.txt"
            file_path.write_text("preserved across msg")

            await kiso_client.post(
                "/sessions",
                json={"session": sess},
                headers=AUTH_HEADER,
            )
            resp = await kiso_client.post(
                "/msg",
                json={"session": sess, "user": "testadmin", "content": "do something"},
                headers=AUTH_HEADER,
            )
            assert resp.status_code == 202

            try:
                await wait_for_worker_idle(kiso_client, sess, timeout=15.0)
            except TimeoutError:
                pass
            await _cleanup_workers(sess)

            # File still there with original content
            assert file_path.exists(), "uploads/input.txt vanished after /msg flow"
            assert file_path.read_text() == "preserved across msg"


class TestPublishedFilePersistence:
    """M1274: published file persistence and identity across plans.

    Unit-level coverage for the /pub/{token}/{filename} endpoint is in
    tests/test_published.py (~15 tests). This class covers the
    integration contract that an already-published file is preserved
    across a subsequent /msg flow — the runtime does not duplicate or
    re-download it.
    """

    async def test_published_file_persists_across_msg_flow(
        self, kiso_client: httpx.AsyncClient, tmp_path: Path,
    ):
        """A file in `pub/` is still present and accessible after a
        /msg flow runs through. Inode unchanged → no duplication."""
        from kiso.pub import pub_token

        sess = "pub-persist"
        with patch("kiso.worker.utils.KISO_DIR", tmp_path), \
             patch("kiso.main.KISO_DIR", tmp_path), \
             patch("kiso.pub.KISO_DIR", tmp_path):
            ws = _session_workspace(sess)
            pub_file = ws / "pub" / "report.txt"
            pub_file.write_text("published artifact content")
            inode_before = pub_file.stat().st_ino

            from kiso.main import app
            token = pub_token(sess, app.state.config)

            # File is reachable via /pub/ before the flow
            resp_before = await kiso_client.get(f"/pub/{token}/report.txt")
            assert resp_before.status_code == 200
            assert resp_before.text == "published artifact content"

            # Run a /msg flow
            await kiso_client.post(
                "/sessions",
                json={"session": sess},
                headers=AUTH_HEADER,
            )
            resp = await kiso_client.post(
                "/msg",
                json={"session": sess, "user": "testadmin", "content": "do something"},
                headers=AUTH_HEADER,
            )
            assert resp.status_code == 202
            try:
                await wait_for_worker_idle(kiso_client, sess, timeout=15.0)
            except TimeoutError:
                pass
            await _cleanup_workers(sess)

            # File still reachable, same content, same inode
            assert pub_file.exists()
            assert pub_file.read_text() == "published artifact content"
            assert pub_file.stat().st_ino == inode_before, (
                "pub file was duplicated/replaced — inode changed"
            )

            resp_after = await kiso_client.get(f"/pub/{token}/report.txt")
            assert resp_after.status_code == 200
            assert resp_after.text == "published artifact content"
