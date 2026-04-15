"""Internal subprocess helpers shared across modules.

Module-private (note the leading underscore). Existing call sites
in :mod:`kiso.hooks` and :mod:`kiso.tool_repair` import the
helpers below; do not add anything here that should be part of
the public surface.
"""

from __future__ import annotations

import asyncio
import os
import signal


async def communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    input_bytes: bytes | None,
    timeout: float,
) -> tuple[bytes, bytes]:
    """Bounded ``proc.communicate(...)`` that kills + reaps on timeout.

     fix. The bare ``asyncio.wait_for(proc.communicate(...),
    timeout=...)`` pattern is dangerous: on timeout, ``wait_for``
    cancels the inner ``communicate()`` coroutine, which leaves
    the StreamReaders for stdout/stderr in a cancelled-suspended
    state. The ``_UnixSubprocessTransport`` then can't detect
    EOF on the proc's pipes (no one is reading them), so a
    subsequent ``proc.wait()`` blocks until the process exits
    naturally — defeating the timeout entirely. Worse, the
    transport survives in memory until GC, where its
    ``__del__`` tries to ``call_soon`` on a closed event loop
    (``Exception ignored: RuntimeError: Event loop is closed``).

    This helper avoids the cancel-then-wait pattern. Instead it
    races ``proc.communicate(input)`` against an
    ``asyncio.sleep(timeout)`` timer using ``asyncio.wait``. On
    timeout it sends SIGKILL to the proc — which causes the
    kernel to close the proc's pipe ends — then **awaits the
    original communicate task to completion**. With the proc
    dying and its pipes closed, the StreamReaders see EOF and
    ``communicate()`` returns naturally. The transport unwinds
    cleanly, ``proc.wait()`` is non-blocking, and the function
    raises :class:`asyncio.TimeoutError` exactly as callers
    expect.

    Behavior on the success path is identical to the bare
    ``wait_for`` form: returns ``(stdout, stderr)`` unchanged.
    """
    comm_task = asyncio.ensure_future(proc.communicate(input_bytes))
    timer_task = asyncio.ensure_future(asyncio.sleep(timeout))
    try:
        done, _pending = await asyncio.wait(
            {comm_task, timer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        # Outer cancellation: tear down both tasks before propagating.
        comm_task.cancel()
        timer_task.cancel()
        raise

    if comm_task in done:
        # Happy path: communicate finished within the timeout.
        timer_task.cancel()
        try:
            await timer_task
        except BaseException:
            pass
        return comm_task.result()

    # Timeout: send SIGKILL to the entire process group, then
    # drain comm_task naturally. Killing the whole group handles
    # the case where the proc is a shell that forked a child
    # (e.g. `sh -c "sleep 30"`) — SIGKILLing only the shell would
    # orphan the child, which keeps the inherited stdout/stderr
    # pipes open and prevents the parent's StreamReaders from
    # ever seeing EOF.
    #
    # killpg requires the proc to be its own process-group
    # leader. Callers must pass ``start_new_session=True`` (or
    # ``process_group=0``) when creating the proc; the helper
    # falls back to ``proc.kill()`` if getpgid fails.
    _kill_process_group(proc)
    try:
        await comm_task
    except BaseException:
        pass
    try:
        await proc.wait()
    except BaseException:
        pass
    raise asyncio.TimeoutError()


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the entire process group of *proc*, with fallback.

    Best effort. ``ProcessLookupError`` (proc already dead) and
    ``PermissionError`` (no perms on the pgid) are swallowed.
    Falls back to ``proc.kill()`` if ``getpgid`` fails.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return
    # Safety: never killpg our own group (would kill ourselves).
    if pgid == os.getpgid(0):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
