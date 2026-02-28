"""Skill task handler for the kiso worker."""

from __future__ import annotations

import json
from pathlib import Path

from kiso.skills import build_skill_env, build_skill_input

from kiso.worker.utils import _run_subprocess, _session_workspace


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
    input_bytes = json.dumps(input_data).encode()

    return await _run_subprocess(
        [str(venv_python), str(run_py)],
        env=env,
        timeout=timeout,
        cwd=str(workspace),
        stdin_data=input_bytes,
        uid=sandbox_uid,
        max_output_size=max_output_size,
    )
