"""F3-F4: System capability functional tests.

F3: SSH key display.
F4: Git clone + intelligent file editing (aider wrapper) + push.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import (
    LLM_INSTALL_TIMEOUT,
    LLM_SINGLE_PLAN_TIMEOUT,
)
from tests.functional.conftest import (
    assert_italian,
    assert_no_failure_language,
)

pytestmark = pytest.mark.functional

SSH_KEY_RE = re.compile(r"ssh-(ed25519|rsa|ecdsa)\s+[A-Za-z0-9+/=]{20,}")


# ---------------------------------------------------------------------------
# F3 — SSH key display
# ---------------------------------------------------------------------------


class TestF3SSHKey:
    """Ask kiso to show its SSH public key."""

    async def test_ssh_key_display(self, run_message):
        """What: System introspection test -- ask the agent to display its SSH public key.

        Why: Validates the exec pipeline for system queries. The agent must find and
        display the SSH key generated in the test fixture, proving it can execute
        shell commands and return structured system information.
        Expects: Plan succeeds, Italian response, SSH key in standard format
        (ssh-ed25519/rsa/ecdsa + base64) present in task outputs.
        """
        result = await run_message(
            "dammi la tua chiave ssh",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian (use last_plan to exclude English replan notifications)
        assert_italian(result.last_plan_msg_output)
        assert_no_failure_language(result.last_plan_msg_output)

        # do not assert on the presence of an exec task. The SSH
        # public key is in boot facts, so the classifier may legitimately
        # route this query to the chat_kb fast path (single msg task) or
        # the planner may emit an exec task to read the file. Both paths
        # are correct as long as the actual key appears in the output —
        # which the SSH_KEY_RE assertion below already enforces.

        # SSH key is present somewhere in task outputs (msg or exec)
        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
        )
        assert SSH_KEY_RE.search(all_output), (
            f"No SSH key found in output. "
            f"Expected ssh-(ed25519|rsa|ecdsa) format. "
            f"Output excerpt: {all_output[:500]}"
        )


# ---------------------------------------------------------------------------
# F4 — Git clone + edit with aider + push
# ---------------------------------------------------------------------------


class TestF4GitAiderPush:
    """Clone a repo, edit a file intelligently, and push."""

    @pytest.mark.destructive
    async def test_git_clone_edit_push(self, run_message):
        """What: Full git workflow test: clone, edit with aider wrapper, push to remote.

        Why: Validates the most complex multi-wrapper scenario (git + aider). Tests that
        Kiso can clone a repository, make intelligent edits, and push changes. This
        is destructive -- it pushes to a real remote branch.
        Expects: Plan succeeds, Italian response, git push indicators in output,
        aider wrapper or direct editing evidence in task outputs.
        """
        result = await run_message(
            "clona git@github.com:kiso-run/core.git e sul branch test "
            "aggiorna il timestamp in docs/testing.md e pushalo online",
            timeout=LLM_INSTALL_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian (use last_plan to exclude English replan notifications)
        assert_italian(result.last_plan_msg_output)
        assert_no_failure_language(result.last_plan_msg_output)

        # Verify git push happened: check all task outputs for push confirmation
        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
        ).lower()
        push_indicators = ("push", "remote", "->", "branch", "test")
        assert any(kw in all_output for kw in push_indicators), (
            f"No git push indicators in output: {all_output[:500]}"
        )
        task_blob = "\n".join(
            (t.get("detail") or "") + "\n" + (t.get("command") or "")
            for t in result.tasks
        ).lower()
        assert "docs/testing.md" in task_blob, (
            f"Expected git workflow to target docs/testing.md, got: {task_blob[:500]}"
        )

        # Check that aider wrapper was used (preferred) or exec editing was done
        wrapper_names = [
            t.get("wrapper") for t in result.tool_tasks()
            if t.get("wrapper")
        ]
        exec_outputs = "\n".join(
            t.get("output") or "" for t in result.tasks
            if t.get("type") == "exec"
        ).lower()
        aider_used = "aider" in wrapper_names
        editing_done = any(
            kw in exec_outputs
            for kw in ("sed", "echo", "tee", "testing.md", "timestamp", "date")
        )
        assert aider_used or editing_done, (
            f"Neither aider wrapper nor direct editing detected. "
            f"Wrappers used: {wrapper_names}, exec output excerpt: {exec_outputs[:300]}"
        )
