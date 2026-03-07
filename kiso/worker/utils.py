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
    cwd: str,
    shell: bool = False,
    stdin_data: bytes | None = None,
    uid: int | None = None,
    max_output_size: int = 0,
) -> tuple[str, str, bool, int]:
    """Run a subprocess and return its output.

    Args:
        cmd: Shell command string (shell=True) or list of args (shell=False).
        env: Subprocess environment dict.
        cwd: Working directory for the subprocess.
        shell: If True, run via ``bash -c``; else create_subprocess_exec.
        stdin_data: Optional bytes to pipe via stdin.
        uid: If set, run the subprocess as this user ID.
        max_output_size: If > 0, truncate stdout/stderr to this many characters.

    Returns:
        (stdout, stderr, success, exit_code) where success is True iff
        returncode == 0.  exit_code is the raw return code (negative for
        signals, -1 for OSError).
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
            # Use bash explicitly — /bin/sh is dash on Debian/Ubuntu and rejects
            # bashisms (<<<, [[ ]], process substitution) that LLMs generate.
            proc = await asyncio.create_subprocess_exec("bash", "-c", cmd, **kwargs)
        else:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        stdout_bytes, stderr_bytes = await proc.communicate(input=stdin_data)
    except OSError as e:
        return "", f"Executable not found: {e}", False, -1

    stdout = _truncate_output(stdout_bytes.decode(errors="replace"), max_output_size)
    stderr = _truncate_output(stderr_bytes.decode(errors="replace"), max_output_size)
    rc = proc.returncode or 0
    return stdout, stderr, rc == 0, rc


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


async def _write_plan_outputs(session: str, plan_outputs: list[dict]) -> None:
    """Write plan_outputs.json to the session workspace's .kiso/ directory."""
    workspace = _session_workspace(session)
    kiso_dir = workspace / ".kiso"
    kiso_dir.mkdir(exist_ok=True)
    path = kiso_dir / "plan_outputs.json"
    content = json.dumps(plan_outputs, indent=2, ensure_ascii=False)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, path.write_text, content, "utf-8")


async def _cleanup_plan_outputs(session: str) -> None:
    """Remove plan_outputs.json after plan completion."""
    workspace = _session_workspace(session)
    outputs_file = workspace / ".kiso" / "plan_outputs.json"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: outputs_file.unlink(missing_ok=True))


def _ensure_sandbox_user_sync(session: str) -> int | None:
    """Synchronous helper: create or reuse a per-session Linux user."""
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


async def _ensure_sandbox_user(session: str) -> int | None:
    """Create or reuse a per-session Linux user. Returns UID or None on failure."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ensure_sandbox_user_sync, session)


def _truncate_output(text: str, limit: int) -> str:
    """Truncate text to *limit* characters, appending a marker if truncated."""
    if limit > 0 and len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def _build_exec_env() -> dict[str, str]:
    """Build the exec subprocess environment.

    The env dict is constructed from scratch (not via dict(os.environ)) so
    dangerous loader variables like LD_PRELOAD, LD_LIBRARY_PATH, PYTHONPATH
    are never inherited from the parent process. Only the vars explicitly
    listed below are passed to the child:

    - PATH: prepend sys/bin if it exists
    - HOME: real home directory (so ``Path.home() / ".kiso"`` resolves correctly
      in child processes like ``kiso skill install``)
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

    # Use the real home directory, NOT KISO_DIR. If HOME were set to KISO_DIR
    # (/root/.kiso inside Docker), any child process computing
    # Path.home() / ".kiso" would resolve to /root/.kiso/.kiso (double nesting).
    env["HOME"] = str(Path.home())

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


_REPLAN_OUTPUT_LIMIT = 2000      # chars per exec/skill task output
_REPLAN_SEARCH_OUTPUT_LIMIT = 2000  # chars per search task output
_REPLAN_CONTEXT_CHAR_BUDGET = 20000  # ~5000 tokens total
_LARGE_OUTPUT_THRESHOLD = 4096   # chars — above this, save to file
_LARGE_OUTPUT_HEAD = 500         # chars to keep inline as preview


def _save_large_output(session: str, task_index: int, output: str) -> str:
    """Save large output to a workspace file; return replacement text with path.

    If the output is below ``_LARGE_OUTPUT_THRESHOLD``, return it unchanged.
    Otherwise write to ``{workspace}/.kiso/task_outputs/task_{index}.txt``
    and return a short reference with the first ``_LARGE_OUTPUT_HEAD`` chars.
    """
    if len(output) <= _LARGE_OUTPUT_THRESHOLD:
        return output
    workspace = _session_workspace(session)
    out_dir = workspace / ".kiso" / "task_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"task_{task_index}.txt"
    path.write_text(output, encoding="utf-8")
    head = output[:_LARGE_OUTPUT_HEAD]
    return (
        f"[Full output saved to {path} ({len(output)} chars). "
        f"Use cat/grep on this file to extract data.]\n{head}\n... (truncated)"
    )


def _smart_truncate(text: str, limit: int) -> str:
    """Truncate *text* to *limit* chars, cutting at a newline boundary."""
    if len(text) <= limit:
        return text
    # Find last newline within limit
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut] + "\n... (truncated)"


def _extract_confirmed_facts(completed: list[dict]) -> list[str]:
    """Best-effort extraction of confirmed facts from completed task outputs.

    Scans outputs for recognisable patterns:
    - Reviewer summaries from successful tasks (highest priority)
    - JSON with "name"/"version" keys (registry responses) → skill/connector names
    - Lines containing "installed" or "available" → installation status
    - For other outputs, extract the first non-empty line as a finding
    """
    facts: list[str] = []
    seen: set[str] = set()

    # Priority 1: reviewer summaries from completed tasks (most reliable)
    for t in completed:
        summary = t.get("reviewer_summary")
        if summary and summary not in seen:
            facts.append(summary)
            seen.add(summary)

    for t in completed:
        out = (t.get("output") or "").strip()
        if not out:
            continue

        # Try JSON parsing for registry-like outputs
        try:
            data = json.loads(out)
            if isinstance(data, dict) and "name" in data:
                fact = f"Skill/connector '{data['name']}' found in registry"
                if "version" in data:
                    fact += f" (v{data['version']})"
                if fact not in seen:
                    facts.append(fact)
                    seen.add(fact)
                continue
            if isinstance(data, list):
                names = [item.get("name") for item in data if isinstance(item, dict) and "name" in item]
                if names:
                    fact = f"Registry contains: {', '.join(names[:10])}"
                    if fact not in seen:
                        facts.append(fact)
                        seen.add(fact)
                    continue
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        # Check for install/status lines
        for line in out.split("\n")[:20]:
            line_lower = line.strip().lower()
            if not line_lower:
                continue
            if "installed" in line_lower or "available" in line_lower:
                fact = line.strip()[:200]
                if fact not in seen:
                    facts.append(fact)
                    seen.add(fact)
                break
            if "not found" in line_lower or "error" in line_lower:
                fact = line.strip()[:200]
                if fact not in seen:
                    facts.append(fact)
                    seen.add(fact)
                break

        # For short outputs (< 200 chars), use the whole thing as a fact
        if not facts or (out[:200] not in seen and len(out) < 200 and t.get("status") == "done"):
            first_line = out.split("\n")[0].strip()[:200]
            if first_line and first_line not in seen:
                facts.append(first_line)
                seen.add(first_line)

    return facts[:15]  # Cap at 15 facts


def _build_replan_context(
    completed: list[dict],
    remaining: list[dict],
    replan_reason: str,
    replan_history: list[dict],
) -> str:
    """Build extra context for replanning."""
    parts: list[str] = []

    # Collect all retry hints from replan history — prominent section at top (M147)
    all_hints: list[str] = []
    seen_hints: set[str] = set()
    for h in replan_history:
        for hint in h.get("retry_hints", []):
            if hint not in seen_hints:
                all_hints.append(hint)
                seen_hints.add(hint)
    if all_hints:
        bullets = "\n".join(f"- {h}" for h in all_hints)
        parts.append(
            "## Suggested Fixes (from reviewer — execute these, do NOT re-investigate)\n"
            + bullets
        )

    # Extract confirmed facts from all completed tasks (current + history)
    all_completed = list(completed)
    for h in replan_history:
        # Reconstruct minimal task dicts from key_outputs for fact extraction
        for ko in h.get("key_outputs", []):
            # key_outputs are formatted as "[type] output_text"
            if ko.startswith("[") and "] " in ko:
                out_text = ko[ko.index("] ") + 2:]
                all_completed.append({"type": "exec", "output": out_text, "status": "done"})
    confirmed = _extract_confirmed_facts(all_completed)
    if confirmed:
        bullets = "\n".join(f"- {f}" for f in confirmed)
        parts.append(
            "## Confirmed Facts (DO NOT re-verify these — they are already established)\n"
            + bullets
        )

    if completed:
        items = []
        total_chars = 0
        for t in completed:
            limit = _REPLAN_SEARCH_OUTPUT_LIMIT if t.get("type") == "search" else _REPLAN_OUTPUT_LIMIT
            if total_chars >= _REPLAN_CONTEXT_CHAR_BUDGET:
                # Over budget — summarize remaining as one-liners
                items.append(f"- [{t['type']}] {t['detail']}: {t['status']}")
                continue
            # Prefer reviewer summary over truncated raw output (M146)
            reviewer_summary = t.get("reviewer_summary")
            if reviewer_summary:
                out_fenced = f"Summary: {reviewer_summary}"
            else:
                raw_out = t.get("output") or ""
                out = _smart_truncate(raw_out, limit)
                out_fenced = fence_content(out, "TASK_OUTPUT") if out else "(no output)"
            item = f"- [{t['type']}] {t['detail']}: {t['status']} →\n{out_fenced}"
            items.append(item)
            total_chars += len(item)
        parts.append("## Completed Tasks\n" + "\n".join(items))

    if remaining:
        items = [f"- [{t['type']}] {t['detail']}" for t in remaining]
        parts.append("## Remaining Tasks (not executed)\n" + "\n".join(items))

    parts.append(f"## Failure Reason\n{replan_reason}")

    if replan_history:
        _HISTORY_OUTPUT_BUDGET = 3000  # max chars for all key_outputs across history
        items = []
        output_chars = 0
        for h in replan_history:
            tried = ", ".join(h.get("what_was_tried", [])) or "nothing"
            entry = f"- Goal: {h['goal']}, Tried: {tried}, Failure: {h['failure']}"
            # Surface reviewer retry hints (M145)
            for hint in h.get("retry_hints", []):
                entry += f"\n  Reviewer hint: {hint}"
            key_outputs = h.get("key_outputs", [])
            if key_outputs and output_chars < _HISTORY_OUTPUT_BUDGET:
                for ko in key_outputs:
                    budget_remaining = _HISTORY_OUTPUT_BUDGET - output_chars
                    if budget_remaining <= 0:
                        break
                    truncated = _smart_truncate(ko, min(budget_remaining, 500))
                    entry += f"\n  Output: {truncated}"
                    output_chars += len(truncated)
            items.append(entry)
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
