"""F3-F4: System capability functional tests.

F3: SSH key display.
F4: Git clone + intelligent file editing (aider skill) + push.
"""

from __future__ import annotations

import re

import pytest

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
        result = await run_message(
            "dammi la tua chiave ssh",
            timeout=120,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian
        assert_italian(result.msg_output)
        assert_no_failure_language(result.msg_output)

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
        result = await run_message(
            "clona git@github.com:kiso-run/core.git e sul branch test "
            "aggiorna il timestamp in docs/testing.md e pushalo online",
            timeout=600,  # aider install + git operations
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )

        # Response is in Italian
        assert_italian(result.msg_output)
        assert_no_failure_language(result.msg_output)

        # Verify git push happened: check all task outputs for push confirmation
        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
        ).lower()
        push_indicators = ("push", "remote", "->", "branch", "test")
        assert any(kw in all_output for kw in push_indicators), (
            f"No git push indicators in output: {all_output[:500]}"
        )

        # Check that aider skill was used (preferred) or exec editing was done
        tool_names = [
            t.get("skill") for t in result.tool_tasks()
            if t.get("skill")
        ]
        exec_outputs = "\n".join(
            t.get("output") or "" for t in result.tasks
            if t.get("type") == "exec"
        ).lower()
        aider_used = "aider" in tool_names
        editing_done = any(
            kw in exec_outputs
            for kw in ("sed", "echo", "tee", "testing.md", "timestamp", "date")
        )
        assert aider_used or editing_done, (
            f"Neither aider skill nor direct editing detected. "
            f"Skills used: {tool_names}, exec output excerpt: {exec_outputs[:300]}"
        )
