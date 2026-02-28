"""Exec task handler for the kiso worker."""

from __future__ import annotations

from kiso.security import check_command_deny_list

from kiso.worker.utils import _build_exec_env, _run_subprocess, _session_workspace


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

    return await _run_subprocess(
        detail,
        env=clean_env,
        timeout=timeout,
        cwd=str(workspace),
        shell=True,
        uid=sandbox_uid,
        max_output_size=max_output_size,
    )
