"""Exec task handler for the kiso worker."""

from __future__ import annotations

import asyncio

from kiso.security import check_command_deny_list

from kiso.worker.utils import _build_exec_env, _session_workspace, _truncate_output


async def _exec_task(
    session: str, detail: str, timeout: int, sandbox_uid: int | None = None,
    max_output_size: int = 0,
) -> tuple[str, str, bool]:
    """Run a shell command. Returns (stdout, stderr, success).

    When *max_output_size* > 0, stdout and stderr are each truncated to
    that many characters to prevent memory exhaustion from oversized output.
    """
    denial = check_command_deny_list(detail)
    if denial:
        return "", denial, False

    workspace = _session_workspace(session)
    clean_env = _build_exec_env()

    try:
        kwargs: dict = dict(
            cwd=str(workspace),
            env=clean_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if sandbox_uid is not None:
            kwargs["user"] = sandbox_uid
        proc = await asyncio.create_subprocess_shell(detail, **kwargs)
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "", "Timed out", False

    stdout = _truncate_output(stdout_bytes.decode(errors="replace"), max_output_size)
    stderr = _truncate_output(stderr_bytes.decode(errors="replace"), max_output_size)
    success = proc.returncode == 0
    return stdout, stderr, success
