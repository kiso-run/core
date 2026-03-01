"""Shared helpers for the kiso.worker package."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
from pathlib import Path

from kiso.config import KISO_DIR, Config
from kiso.pub import pub_token
from kiso.security import fence_content

log = logging.getLogger(__name__)


async def _run_subprocess(
    cmd,
    *,
    env: dict,
    timeout: int | float,
    cwd: str,
    shell: bool = False,
    stdin_data: bytes | None = None,
    uid: int | None = None,
    max_output_size: int = 0,
) -> tuple[str, str, bool]:
    """Run a subprocess with timeout and output handling.

    Args:
        cmd: Shell command string (shell=True) or list of args (shell=False).
        env: Subprocess environment dict.
        timeout: Seconds before the subprocess is killed.
        cwd: Working directory for the subprocess.
        shell: If True, use create_subprocess_shell; else create_subprocess_exec.
        stdin_data: Optional bytes to pipe via stdin.
        uid: If set, run the subprocess as this user ID.
        max_output_size: If > 0, truncate stdout/stderr to this many characters.

    Returns:
        (stdout, stderr, success) where success is True iff returncode == 0.
    """
    kwargs: dict = dict(
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if stdin_data is not None:
        kwargs["stdin"] = asyncio.subprocess.PIPE
    if uid is not None:
        kwargs["user"] = uid

    try:
        if shell:
            proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
        else:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_data), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "", "Timed out", False
    except OSError as e:
        return "", f"Executable not found: {e}", False

    stdout = _truncate_output(stdout_bytes.decode(errors="replace"), max_output_size)
    stderr = _truncate_output(stderr_bytes.decode(errors="replace"), max_output_size)
    return stdout, stderr, proc.returncode == 0


def _session_workspace(session: str, sandbox_uid: int | None = None) -> Path:
    """Return and ensure the session workspace directory exists."""
    workspace = KISO_DIR / "sessions" / session
    workspace.mkdir(parents=True, exist_ok=True)
    pub_dir = workspace / "pub"
    pub_dir.mkdir(exist_ok=True)
    uploads_dir = workspace / "uploads"
    uploads_dir.mkdir(exist_ok=True)
    if sandbox_uid is not None:
        try:
            os.chown(workspace, sandbox_uid, sandbox_uid)
            os.chown(pub_dir, sandbox_uid, sandbox_uid)
            os.chown(uploads_dir, sandbox_uid, sandbox_uid)
            os.chmod(workspace, 0o700)
        except OSError as exc:
            log.warning("Cannot set workspace ownership for %s: %s", session, exc)
    return workspace


def _write_plan_outputs(session: str, plan_outputs: list[dict]) -> None:
    """Write plan_outputs.json to the session workspace's .kiso/ directory."""
    workspace = _session_workspace(session)
    kiso_dir = workspace / ".kiso"
    kiso_dir.mkdir(exist_ok=True)
    (kiso_dir / "plan_outputs.json").write_text(
        json.dumps(plan_outputs, indent=2, ensure_ascii=False)
    )


def _cleanup_plan_outputs(session: str) -> None:
    """Remove plan_outputs.json after plan completion."""
    workspace = _session_workspace(session)
    outputs_file = workspace / ".kiso" / "plan_outputs.json"
    if outputs_file.exists():
        outputs_file.unlink()


def _ensure_sandbox_user(session: str) -> int | None:
    """Create or reuse a per-session Linux user. Returns UID or None on failure."""
    import hashlib
    import subprocess

    h = hashlib.sha256(session.encode()).hexdigest()[:12]
    username = f"kiso-s-{h}"
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError:
        pass
    try:
        subprocess.run(
            ["useradd", "--system", "--no-create-home",
             "--shell", "/usr/sbin/nologin", username],
            check=True, capture_output=True,
        )
        return pwd.getpwnam(username).pw_uid
    except (subprocess.CalledProcessError, KeyError, FileNotFoundError) as exc:
        log.warning("Cannot create sandbox user '%s': %s", username, exc)
        return None


def _truncate_output(text: str, limit: int) -> str:
    """Truncate text to *limit* characters, appending a marker if truncated."""
    if limit > 0 and len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def _build_exec_env() -> dict[str, str]:
    """Build the exec subprocess environment.

    - PATH: prepend sys/bin if it exists
    - HOME: set to KISO_DIR (for tools that need ~)
    - GIT_CONFIG_GLOBAL: point to sys/gitconfig if it exists
    - GIT_SSH_COMMAND: use sys/ssh config if it exists
    """
    sys_dir = KISO_DIR / "sys"
    sys_bin = sys_dir / "bin"
    base_path = os.environ.get("PATH", "/usr/bin:/bin")

    env: dict[str, str] = {}

    if sys_bin.is_dir():
        env["PATH"] = f"{sys_bin}:{base_path}"
    else:
        env["PATH"] = base_path

    env["HOME"] = str(KISO_DIR)

    gitconfig = sys_dir / "gitconfig"
    if gitconfig.is_file():
        env["GIT_CONFIG_GLOBAL"] = str(gitconfig)

    ssh_dir = sys_dir / "ssh"
    if ssh_dir.is_dir() and (ssh_dir / "config").is_file() and (ssh_dir / "id_ed25519").is_file():
        env["GIT_SSH_COMMAND"] = f"ssh -F {ssh_dir}/config -o UserKnownHostsFile={ssh_dir}/known_hosts -i {ssh_dir}/id_ed25519"

    return env


def _report_pub_files(session: str, config: Config) -> list[dict]:
    """List files in pub/ and return their public URLs."""
    pub_dir = _session_workspace(session) / "pub"
    if not pub_dir.is_dir():
        return []
    try:
        token = pub_token(session, config)
    except ValueError as exc:
        log.warning("Cannot generate pub URLs: %s", exc)
        return []
    _MAX_PUB_SCAN = 1000
    all_paths: list[Path] = []
    truncated = False
    for p in pub_dir.rglob("*"):
        if len(all_paths) >= _MAX_PUB_SCAN:
            truncated = True
            break
        all_paths.append(p)
    if truncated:
        log.warning("pub/ for session %r has >%d entries, listing truncated", session, _MAX_PUB_SCAN)
    results = []
    for f in sorted(all_paths):
        if f.is_file():
            rel = f.relative_to(pub_dir)
            results.append({
                "filename": str(rel),
                "url": f"/pub/{token}/{rel}",
            })
    return results


def _format_plan_outputs_for_msg(plan_outputs: list[dict]) -> str:
    """Format plan_outputs as readable text for the worker LLM prompt."""
    if not plan_outputs:
        return ""
    parts: list[str] = []
    for entry in plan_outputs:
        header = f"[{entry['index']}] {entry['type']}: {entry['detail']}"
        output = entry.get("output") or "(no output)"
        status = entry["status"]
        parts.append(f"{header}\nStatus: {status}\n{fence_content(output, 'TASK_OUTPUT')}")
    return "\n\n".join(parts)


def _build_replan_context(
    completed: list[dict],
    remaining: list[dict],
    replan_reason: str,
    replan_history: list[dict],
) -> str:
    """Build extra context for replanning."""
    parts: list[str] = []

    if completed:
        items = []
        for t in completed:
            limit = 4000 if t.get("type") == "search" else 500
            out = (t.get("output") or "")[:limit]
            out_fenced = fence_content(out, "TASK_OUTPUT") if out else "(no output)"
            items.append(f"- [{t['type']}] {t['detail']}: {t['status']} →\n{out_fenced}")
        parts.append("## Completed Tasks\n" + "\n".join(items))

    if remaining:
        items = [f"- [{t['type']}] {t['detail']}" for t in remaining]
        parts.append("## Remaining Tasks (not executed)\n" + "\n".join(items))

    parts.append(f"## Failure Reason\n{replan_reason}")

    if replan_history:
        items = []
        for h in replan_history:
            tried = ", ".join(h.get("what_was_tried", [])) or "nothing"
            items.append(f"- Goal: {h['goal']}, Tried: {tried}, Failure: {h['failure']}")
        parts.append(
            "## Previous Replan Attempts (DO NOT repeat these approaches)\n"
            + "\n".join(items)
        )

    return "\n\n".join(parts)


def _build_cancel_summary(
    completed: list[dict], remaining: list[dict], goal: str,
) -> str:
    """Build a detail string for the worker LLM summarising a cancel."""
    parts: list[str] = [f"The user cancelled the plan: {goal}"]

    if completed:
        items = [f"- [{t['type']}] {t['detail']}" for t in completed]
        parts.append(f"Completed ({len(completed)}):\n" + "\n".join(items))
    else:
        parts.append("No tasks were completed.")

    if remaining:
        items = [f"- [{t['type']}] {t['detail']}" for t in remaining]
        parts.append(f"Skipped ({len(remaining)}):\n" + "\n".join(items))

    parts.append(
        "Generate a brief message: what was done, what wasn't, "
        "and suggest next steps."
    )
    return "\n\n".join(parts)


def _build_failure_summary(
    completed: list[dict], remaining: list[dict], goal: str,
    reason: str | None = None,
) -> str:
    """Build a detail string for the messenger LLM summarising a plan failure."""
    parts: list[str] = [f"The plan failed: {goal}"]

    if reason:
        parts.append(f"Failure reason: {reason}")

    if completed:
        items = [f"- [{t['type']}] {t['detail']}" for t in completed]
        parts.append(f"Completed ({len(completed)}):\n" + "\n".join(items))
    else:
        parts.append("No tasks were completed.")

    if remaining:
        items = [f"- [{t['type']}] {t['detail']}" for t in remaining]
        parts.append(f"Failed/Skipped ({len(remaining)}):\n" + "\n".join(items))

    parts.append(
        "Generate a brief message explaining what went wrong "
        "and suggest next steps."
    )
    return "\n\n".join(parts)
