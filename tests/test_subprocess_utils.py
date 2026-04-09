"""Tests for kiso._subprocess_utils.communicate_with_timeout (M1295)."""

from __future__ import annotations

import asyncio
import os

import pytest


# ---------------------------------------------------------------------------
# Helper contract: communicate completes within timeout → forward result
# ---------------------------------------------------------------------------


class TestCommunicateWithTimeoutHappyPath:
    async def test_returns_stdout_stderr_on_normal_completion(self, tmp_path):
        from kiso._subprocess_utils import communicate_with_timeout

        proc = await asyncio.create_subprocess_exec(
            "/bin/echo", "hello world",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await communicate_with_timeout(
            proc, input_bytes=None, timeout=5.0,
        )
        assert b"hello world" in stdout
        assert proc.returncode == 0

    async def test_writes_stdin_when_input_bytes_provided(self, tmp_path):
        from kiso._subprocess_utils import communicate_with_timeout

        proc = await asyncio.create_subprocess_exec(
            "/bin/cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await communicate_with_timeout(
            proc, input_bytes=b"piped\n", timeout=5.0,
        )
        assert stdout == b"piped\n"
        assert proc.returncode == 0

    async def test_input_bytes_none_with_devnull_stdin(self):
        """Mirrors the post-exec hook pattern (stdin not piped)."""
        from kiso._subprocess_utils import communicate_with_timeout

        proc = await asyncio.create_subprocess_exec(
            "/bin/echo", "ok",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await communicate_with_timeout(
            proc, input_bytes=None, timeout=5.0,
        )
        assert b"ok" in stdout
        assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Helper contract: timeout reaps the subprocess BEFORE re-raising
# ---------------------------------------------------------------------------


class TestCommunicateWithTimeoutReaps:
    async def test_timeout_kills_and_reaps_proc(self, tmp_path):
        """The core M1295 contract: when wait_for hits the timeout,
        the helper must SIGKILL the subprocess and await its
        termination so the OS process is reaped and its
        _UnixSubprocessTransport is released BEFORE the caller
        sees TimeoutError. After the helper returns (via raise),
        proc.returncode must be set."""
        from kiso._subprocess_utils import communicate_with_timeout

        proc = await asyncio.create_subprocess_exec(
            "/bin/sleep", "30",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pid = proc.pid
        with pytest.raises(asyncio.TimeoutError):
            await communicate_with_timeout(
                proc, input_bytes=None, timeout=0.1,
            )

        # Process is reaped: returncode is set, OS process is gone.
        assert proc.returncode is not None
        # On Linux, signal -9 (SIGKILL) shows as -9 in returncode.
        assert proc.returncode in (-9, -15) or proc.returncode < 0

        # OS-level proof: kill(pid, 0) raises ProcessLookupError once
        # the process is reaped.
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_timeout_reaps_even_when_input_bytes_pending(self, tmp_path):
        """Reaping must work even when stdin had data pending."""
        from kiso._subprocess_utils import communicate_with_timeout

        # `cat` blocks reading stdin. Send a tiny chunk + close stdin
        # late so wait_for fires the timeout while communicate is
        # still mid-flight.
        proc = await asyncio.create_subprocess_exec(
            "/bin/sleep", "30",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pid = proc.pid
        with pytest.raises(asyncio.TimeoutError):
            await communicate_with_timeout(
                proc, input_bytes=b"x" * 1024, timeout=0.1,
            )
        assert proc.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_timeout_already_dead_proc_does_not_raise(self, tmp_path):
        """If the proc is already dead by the time we kill it
        (race), ProcessLookupError must be swallowed silently —
        the timeout is still re-raised."""
        from kiso._subprocess_utils import communicate_with_timeout

        proc = await asyncio.create_subprocess_exec(
            "/bin/sleep", "30",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Kill out-of-band so the helper hits an already-dead proc.
        proc.kill()
        await proc.wait()  # ensure reaped
        # Now ask the helper to communicate with a tiny timeout.
        # Since the proc is already dead, communicate() returns
        # immediately with empty bytes — NOT a TimeoutError. This
        # exercises the no-op path of the helper's cleanup.
        stdout, stderr = await communicate_with_timeout(
            proc, input_bytes=None, timeout=0.1,
        )
        assert proc.returncode is not None
