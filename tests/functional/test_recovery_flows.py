"""M1281 (redesigned in M1287): workspace and file-reuse functional flows.

The original M1281 test_unreachable_then_local_file_recovery tried to
test "recovery semantics" against real LLM by relying on the planner
to choose to replan after a failed first attempt. That oracle is
fundamentally fragile: the LLM is non-deterministic about when it
chooses to replan vs. fail gracefully, and the "recovery semantics"
contract is already deterministically tested in M1275 with mocked
LLM (test_replan_stop.py).

M1287 redesigns this file:

- ``test_workspace_file_discovery`` (replaces
  ``test_unreachable_then_local_file_recovery``): pre-shapes the
  session workspace with a deterministic file, sends an imperative
  prompt that does NOT give the planner permission to fail, and
  asserts that the messenger output contains the file content. The
  planner is free to choose any read strategy (cat, ls, read, etc.).
- ``test_no_redundant_fetch_when_file_exists_locally``: cleaned up to
  use ``assert_no_command_word`` (the M1286 helper) for the
  curl/wget check, eliminating the substring-vs-word-boundary risk
  that M1286's sweep missed.

Recovery semantics (replan-on-failure → stop on stuck) are
deterministically tested in M1275:
``tests/integration/test_replan_stop.py``. They are intentionally
NOT re-tested here at functional tier.

Requires ``--functional`` flag and a running OpenRouter API key.
These tests need Docker + a real LLM and cannot run on a bare host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.worker.utils import _session_workspace

from tests.conftest import LLM_MULTI_PLAN_TIMEOUT
from tests.functional.conftest import (
    assert_no_command_word,
    assert_no_failure_language,
)


pytestmark = pytest.mark.functional


class TestWorkspaceFileDiscovery:
    """Planner can discover and read a file pre-placed in the session
    workspace when given an imperative prompt."""

    async def test_workspace_file_discovery(
        self, run_message, func_session, tmp_path: Path,
    ):
        """Pre-create a file with deterministic content in the
        session workspace, send an imperative prompt that does NOT
        give the planner permission to fail, and assert the
        messenger output contains the file content.

        The planner is free to choose ANY read strategy (cat, ls,
        find, read, etc.). The test does not constrain how the file
        is found — only that the runtime discovers it and surfaces
        its content."""
        # Pre-shape: create the file in the session workspace BEFORE
        # sending any message. Use _session_workspace() so the path
        # resolution matches the worker's view of KISO_DIR (the
        # functional fixture _func_kiso_dir patches KISO_DIR in
        # kiso.worker.utils, not at import time in this module).
        workspace = _session_workspace(func_session)
        test_file = workspace / "data.txt"
        sentinel = "cached recovery sentinel 4242"
        test_file.write_text(sentinel)

        result = await run_message(
            "There is a file called `data.txt` in your current working "
            "directory. Read it and tell me exactly what it contains.",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"workspace discovery failed: "
            f"plans={[p.get('status') for p in result.plans]}"
        )

        # The messenger output must surface the file content
        output = result.last_plan_msg_output.lower()
        assert_no_failure_language(output)
        assert sentinel.lower() in output, (
            f"file content not surfaced in messenger output: "
            f"sentinel={sentinel!r}, output={output[:500]}"
        )

        # File survived the run unchanged (sanity)
        assert test_file.exists()
        assert test_file.read_text() == sentinel


class TestLocalFileReuseAcrossPlans:
    """Local file discovered in prior plan → reused in later plan
    without re-fetching."""

    async def test_no_redundant_fetch_when_file_exists_locally(
        self, run_message, func_session, tmp_path: Path,
    ):
        """First message: produce a local artifact (e.g. write a
        small file via exec). Second message: reference that
        artifact and check that the new plan does NOT issue a
        download/fetch command."""
        await run_message(
            "Create a file called /tmp/kiso-cached-data.txt containing the "
            "text 'cached value 42'.",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        result = await run_message(
            "Read /tmp/kiso-cached-data.txt and tell me what number it "
            "contains. The file already exists locally — do not download "
            "or fetch anything.",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )

        assert result.success

        # M1287: word-boundary check on the command field only.
        # M1286's original sweep missed this test; the previous
        # substring scan over command+detail was vulnerable to the
        # same false-positives M1286 fixed elsewhere ("curly" matches
        # "curl" inside heredoc bodies).
        last_plan_id = result.plans[-1]["id"]
        last_plan_tasks = [
            t for t in result.tasks if t.get("plan_id") == last_plan_id
        ]
        assert_no_command_word(last_plan_tasks, ["curl", "wget"])
