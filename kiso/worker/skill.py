"""Skill task handler for the kiso worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kiso.skills import build_skill_env, build_skill_input

from kiso.worker.utils import _session_workspace, _truncate_output


async def _skill_task(
    session: str,
    skill: dict,
    args: dict,
    plan_outputs: list[dict] | None,
    session_secrets: dict[str, str] | None,
    timeout: int,
    sandbox_uid: int | None = None,
    max_output_size: int = 0,
) -> tuple[str, str, bool]:
    """Run a skill subprocess. Returns (stdout, stderr, success).

    When *max_output_size* > 0, stdout and stderr are each truncated to
    that many characters to prevent memory exhaustion from oversized output.
    """
    workspace = _session_workspace(session)

    input_data = build_skill_input(
        skill, args, session, str(workspace),
        session_secrets=session_secrets,
        plan_outputs=plan_outputs,
    )
    env = build_skill_env(skill)

    skill_path = Path(skill["path"])
    venv_python = skill_path / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path("python3")

    run_py = skill_path / "run.py"

    try:
        skill_kwargs: dict = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env=env,
        )
        if sandbox_uid is not None:
            skill_kwargs["user"] = sandbox_uid
        proc = await asyncio.create_subprocess_exec(
            str(venv_python), str(run_py), **skill_kwargs,
        )
        input_bytes = json.dumps(input_data).encode()
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=input_bytes), timeout=timeout,
        )
    except asyncio.TimeoutError:
        return "", "Timed out", False
    except OSError as e:
        return "", f"Skill executable not found: {e}", False

    stdout = _truncate_output(stdout_bytes.decode(errors="replace"), max_output_size)
    stderr = _truncate_output(stderr_bytes.decode(errors="replace"), max_output_size)
    success = proc.returncode == 0
    return stdout, stderr, success
