"""Tests for kiso.hooks — pre/post execution hooks."""

from __future__ import annotations

import json

import pytest

from kiso.hooks import HookResult, run_post_exec_hooks, run_pre_exec_hooks


@pytest.mark.asyncio
class TestPreExecHooks:
    async def test_no_hooks_allows(self):
        result = await run_pre_exec_hooks([], "ls", "list files", "s1", 1)
        assert result.allowed is True

    async def test_non_blocking_hook_allows_on_nonzero(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o755)
        hooks = [{"command": str(script), "blocking": False}]
        result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
        assert result.allowed is True

    async def test_blocking_hook_denies_on_nonzero(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\necho 'forbidden' >&2\nexit 1\n")
        script.chmod(0o755)
        hooks = [{"command": str(script), "blocking": True}]
        result = await run_pre_exec_hooks(hooks, "rm -rf /", "danger", "s1", 1)
        assert result.allowed is False
        assert "forbidden" in result.message

    async def test_blocking_hook_allows_on_zero(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        hooks = [{"command": str(script), "blocking": True}]
        result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
        assert result.allowed is True

    async def test_hook_receives_context_via_stdin(self, tmp_path):
        output_file = tmp_path / "received.json"
        script = tmp_path / "hook.sh"
        script.write_text(f"#!/bin/sh\ncat > {output_file}\n")
        script.chmod(0o755)
        hooks = [{"command": str(script)}]
        await run_pre_exec_hooks(hooks, "echo hi", "say hello", "sess1", 42)
        ctx = json.loads(output_file.read_text())
        assert ctx["event"] == "pre_exec"
        assert ctx["command"] == "echo hi"
        assert ctx["detail"] == "say hello"
        assert ctx["session"] == "sess1"
        assert ctx["task_id"] == 42

    async def test_hook_timeout_allows(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\nsleep 60\n")
        script.chmod(0o755)
        hooks = [{"command": str(script), "blocking": True}]
        # Patch timeout to 0.1s for fast test
        import kiso.hooks
        orig = kiso.hooks._HOOK_TIMEOUT
        kiso.hooks._HOOK_TIMEOUT = 0.1
        try:
            result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
            assert result.allowed is True  # timeout = allow
        finally:
            kiso.hooks._HOOK_TIMEOUT = orig

    async def test_pre_hook_timeout_reaps_subprocess(self, tmp_path):
        """M1295: when a pre-exec hook times out, the underlying
        subprocess must be killed and reaped before
        run_pre_exec_hooks returns. Otherwise the hook process
        keeps running, the asyncio child watcher keeps a stale
        callback registered for the PID, and the
        ``_UnixSubprocessTransport`` survives until GC, firing
        ``BaseSubprocessTransport.__del__`` after the event loop
        closes (Exception ignored: Event loop is closed).

        Spawns a hook running ``sleep 30`` with a 0.1s timeout,
        then verifies (via ``os.kill(pid, 0)``) that the spawned
        PID no longer exists after the call returns.
        """
        import os
        import asyncio
        import kiso.hooks

        # Wrap create_subprocess_shell to capture the PID of the
        # spawned hook process so we can probe it post-call.
        captured_pids: list[int] = []
        real = asyncio.create_subprocess_shell

        async def spy(*args, **kwargs):
            proc = await real(*args, **kwargs)
            captured_pids.append(proc.pid)
            return proc

        script = tmp_path / "slow_hook.sh"
        script.write_text("#!/bin/sh\nsleep 30\n")
        script.chmod(0o755)
        hooks = [{"command": str(script), "blocking": True}]

        orig_timeout = kiso.hooks._HOOK_TIMEOUT
        kiso.hooks._HOOK_TIMEOUT = 0.1
        orig_create = asyncio.create_subprocess_shell
        asyncio.create_subprocess_shell = spy
        try:
            result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
        finally:
            kiso.hooks._HOOK_TIMEOUT = orig_timeout
            asyncio.create_subprocess_shell = orig_create

        assert result.allowed is True
        assert captured_pids, "expected the hook to spawn a subprocess"
        # Wait briefly so the kernel reflects the kill in /proc.
        await asyncio.sleep(0.05)
        for pid in captured_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_empty_command_skipped(self):
        hooks = [{"command": "", "blocking": True}]
        result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
        assert result.allowed is True

    async def test_multiple_pre_hooks_first_blocks(self, tmp_path):
        """First blocking hook denies, second never runs."""
        marker = tmp_path / "ran"
        script1 = tmp_path / "block.sh"
        script1.write_text("#!/bin/sh\nexit 1\n")
        script1.chmod(0o755)
        script2 = tmp_path / "mark.sh"
        script2.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script2.chmod(0o755)
        hooks = [
            {"command": str(script1), "blocking": True},
            {"command": str(script2), "blocking": True},
        ]
        result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
        assert result.allowed is False
        assert not marker.exists()  # second hook never ran

    async def test_invalid_command_path(self):
        """Non-existent command returns non-zero → blocking hook denies."""
        hooks = [{"command": "/nonexistent/path/hook.sh", "blocking": True}]
        result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
        assert result.allowed is False  # non-zero exit → deny
        assert "not found" in result.message

    async def test_multiple_pre_hooks_all_pass(self, tmp_path):
        """Multiple passing hooks all execute."""
        script = tmp_path / "pass.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        hooks = [
            {"command": str(script), "blocking": True},
            {"command": str(script), "blocking": True},
        ]
        result = await run_pre_exec_hooks(hooks, "ls", "list", "s1", 1)
        assert result.allowed is True


@pytest.mark.asyncio
class TestPostExecHooks:
    async def test_no_hooks_succeeds(self):
        await run_post_exec_hooks([], "ls", "list", "s1", 1, "output", "", 0)

    async def test_hook_receives_context(self, tmp_path):
        output_file = tmp_path / "received.json"
        script = tmp_path / "hook.sh"
        script.write_text(f"#!/bin/sh\ncat > {output_file}\n")
        script.chmod(0o755)
        hooks = [{"command": str(script)}]
        await run_post_exec_hooks(hooks, "echo hi", "say hello", "sess1", 42, "hello", "", 0)
        # Give async subprocess time to complete
        import asyncio
        await asyncio.sleep(0.2)
        ctx = json.loads(output_file.read_text())
        assert ctx["event"] == "post_exec"
        assert ctx["command"] == "echo hi"
        assert ctx["exit_code"] == 0
        assert ctx["stdout"] == "hello"

    async def test_failing_hook_does_not_raise(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o755)
        hooks = [{"command": str(script)}]
        # Should not raise
        await run_post_exec_hooks(hooks, "ls", "list", "s1", 1, "", "", 0)

    async def test_post_hook_timeout_reaps_subprocess(self, tmp_path):
        """M1295: same contract as the pre-exec test, applied to
        post-exec. When a post-exec hook times out, the helper
        must SIGKILL + reap the spawned process. Verified
        OS-level via os.kill(pid, 0)."""
        import os
        import asyncio
        import kiso.hooks

        captured_pids: list[int] = []
        real = asyncio.create_subprocess_shell

        async def spy(*args, **kwargs):
            proc = await real(*args, **kwargs)
            captured_pids.append(proc.pid)
            return proc

        script = tmp_path / "slow_hook.sh"
        script.write_text("#!/bin/sh\nsleep 30\n")
        script.chmod(0o755)
        hooks = [{"command": str(script)}]

        orig_timeout = kiso.hooks._HOOK_TIMEOUT
        kiso.hooks._HOOK_TIMEOUT = 0.1
        orig_create = asyncio.create_subprocess_shell
        asyncio.create_subprocess_shell = spy
        try:
            await run_post_exec_hooks(
                hooks, "ls", "list", "s1", 1, "out", "", 0,
            )
        finally:
            kiso.hooks._HOOK_TIMEOUT = orig_timeout
            asyncio.create_subprocess_shell = orig_create

        assert captured_pids, "expected the hook to spawn a subprocess"
        await asyncio.sleep(0.05)
        for pid in captured_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
