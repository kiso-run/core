"""Tool task handler for the kiso worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kiso.tools import build_tool_env, build_tool_input

from kiso.worker.utils import _run_subprocess, _session_workspace


async def _tool_task(
    session: str,
    tool: dict,
    args: dict,
    plan_outputs: list[dict] | None,
    session_secrets: dict[str, str] | None,
    sandbox_uid: int | None = None,
    max_output_size: int = 0,
    cancel_event: "asyncio.Event | None" = None,
) -> tuple[str, str, bool, int]:
    """Run a tool subprocess. Returns (stdout, stderr, success, exit_code).

    When *max_output_size* > 0, stdout and stderr are each truncated to
    that many characters to prevent memory exhaustion from oversized output.
    """
    workspace = _session_workspace(session)

    input_data = build_tool_input(
        tool, args, session, str(workspace),
        session_secrets=session_secrets,
        plan_outputs=plan_outputs,
    )
    env = build_tool_env(tool)

    tool_path = Path(tool["path"])
    venv_python = tool_path / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path("python3")

    run_py = tool_path / "run.py"
    input_bytes = json.dumps(input_data).encode()

    return await _run_subprocess(
        [str(venv_python), str(run_py)],
        env=env,
        cwd=str(workspace),
        stdin_data=input_bytes,
        uid=sandbox_uid,
        max_output_size=max_output_size,
        cancel_event=cancel_event,
    )
