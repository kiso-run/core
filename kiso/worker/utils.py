"""Shared helpers for the kiso.worker package."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

from kiso.config import KISO_DIR, Config
from kiso.pub import pub_token
from kiso.security import fence_content

log = logging.getLogger(__name__)


async def _run_sync(fn, *args):
    """Run a sync function in the default executor."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


_CANCEL_GRACE_PERIOD = 2  # seconds to wait after SIGTERM before SIGKILL


async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    """Send SIGTERM, wait briefly, then SIGKILL if still alive."""
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_CANCEL_GRACE_PERIOD)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _run_subprocess(
    cmd,
    *,
    env: dict,
    cwd: str,
    shell: bool = False,
    stdin_data: bytes | None = None,
    uid: int | None = None,
    max_output_size: int = 0,
    cancel_event: "asyncio.Event | None" = None,
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
        cancel_event: If set and fired during execution, the subprocess is
            terminated (SIGTERM → SIGKILL) and the function returns with
            exit_code -15.

    Returns:
        (stdout, stderr, success, exit_code) where success is True iff
        returncode == 0.  exit_code is the raw return code (negative for
        signals, -1 for OSError).
    """
    kwargs: dict = dict(
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if uid is not None:
        kwargs["user"] = uid

    try:
        if shell:
            # Use bash explicitly — /bin/sh is dash on Debian/Ubuntu and rejects
            # bashisms (<<<, [[ ]], process substitution) that LLMs generate.
            proc = await asyncio.create_subprocess_exec("bash", "-c", cmd, **kwargs)
        else:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

        # race communicate() against cancel_event
        if cancel_event is not None and cancel_event.is_set():
            # Already cancelled — kill immediately
            await _kill_proc(proc)
            return "", "cancelled", False, -15
        if cancel_event is not None:
            comm_task = asyncio.ensure_future(proc.communicate(input=stdin_data))
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait(
                {comm_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if cancel_task in done:
                # Cancel fired — kill subprocess
                await _kill_proc(proc)
                return "", "cancelled", False, -15
            stdout_bytes, stderr_bytes = comm_task.result()
        else:
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
    await _run_sync(path.write_text, content, "utf-8")


async def _cleanup_plan_outputs(session: str) -> None:
    """Remove plan_outputs.json after plan completion."""
    workspace = _session_workspace(session)
    outputs_file = workspace / ".kiso" / "plan_outputs.json"
    await _run_sync(lambda: outputs_file.unlink(missing_ok=True))


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
    return await _run_sync(_ensure_sandbox_user_sync, session)


def _truncate_output(text: str, limit: int) -> str:
    """Truncate text to *limit* characters, appending a marker if truncated."""
    marker = "\n[truncated]"
    if limit > 0 and len(text) > limit:
        return text[: limit - len(marker)] + marker
    return text


def _build_exec_env() -> dict[str, str]:
    """Build the exec subprocess environment.

    The env dict is constructed from scratch (not via dict(os.environ)) so
    dangerous loader variables like LD_PRELOAD, LD_LIBRARY_PATH, PYTHONPATH
    are never inherited from the parent process. Only the vars explicitly
    listed below are passed to the child:

    - PATH: sys/bin + kiso venv bin + system PATH
    - HOME: real home directory (so ``Path.home() / ".kiso"`` resolves correctly
      in child processes like ``kiso skill install``)
    - GIT_CONFIG_GLOBAL: point to sys/gitconfig if it exists
    - GIT_SSH_COMMAND: use sys/ssh config if it exists
    """
    sys_dir = KISO_DIR / "sys"
    sys_bin = sys_dir / "bin"
    base_path = os.environ.get("PATH", "/usr/bin:/bin")

    # include the kiso process's own venv bin so that entry-point
    # scripts (kiso CLI, uv, etc.) are available to exec tasks.
    venv_bin = str(Path(sys.executable).parent)

    env: dict[str, str] = {}

    path_parts: list[str] = []
    if sys_bin.is_dir():
        path_parts.append(str(sys_bin))
    path_parts.append(venv_bin)
    path_parts.append(base_path)
    env["PATH"] = ":".join(path_parts)

    # Use the real home directory, NOT KISO_DIR. If HOME were set to KISO_DIR
    # (/root/.kiso inside Docker), any child process computing
    # Path.home() / ".kiso" would resolve to /root/.kiso/.kiso (double nesting).
    env["HOME"] = str(Path.home())

    # propagate KISO_HOME so child processes (kiso CLI) resolve KISO_DIR
    # to the same directory as the parent — critical for test isolation.
    env["KISO_HOME"] = str(KISO_DIR)

    gitconfig = sys_dir / "gitconfig"
    if gitconfig.is_file():
        env["GIT_CONFIG_GLOBAL"] = str(gitconfig)

    ssh_dir = sys_dir / "ssh"
    if ssh_dir.is_dir() and (ssh_dir / "config").is_file() and (ssh_dir / "id_ed25519").is_file():
        # quote paths to prevent shell injection if path contains spaces/special chars
        env["GIT_SSH_COMMAND"] = (
            f"ssh -F '{ssh_dir}/config' "
            f"-o UserKnownHostsFile='{ssh_dir}/known_hosts' "
            f"-i '{ssh_dir}/id_ed25519'"
        )

    return env


def _kiso_dir_bytes() -> int | None:
    """Return total bytes used by KISO_DIR (recursive).

    Uses ``du -sb`` for speed; falls back to a Python walk if ``du``
    is unavailable.  Returns *None* on any error.
    """
    try:
        out = subprocess.check_output(
            ["du", "-sb", str(KISO_DIR)],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return int(out.split()[0])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        pass

    # Fallback: walk the tree in Python.
    try:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(KISO_DIR):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total
    except OSError:
        return None


def _check_disk_limit(config: Config) -> str | None:
    """Check KISO_DIR usage against max_disk_gb. Returns error msg or None."""
    max_gb = config.settings.get("max_disk_gb", 32)
    total = _kiso_dir_bytes()
    if total is None:
        return None
    used_gb = total / (1024**3)
    if used_gb > max_gb:
        return f"Disk limit exceeded: {used_gb:.1f} GB used, limit {max_gb} GB"
    return None


def _report_pub_files(
    session: str, config: Config, base_url: str = "",
) -> list[dict]:
    """List files in pub/ and return their public URLs.

    When *base_url* is provided (e.g. ``http://host:8333``), URLs are
    absolute; otherwise they are server-relative (``/pub/…``).
    """
    pub_dir = _session_workspace(session) / "pub"
    if not pub_dir.is_dir():
        return []
    try:
        token = pub_token(session, config)
    except ValueError as exc:
        log.warning("Cannot generate pub URLs: %s", exc)
        return []
    all_paths: list[Path] = []
    truncated = False
    for p in pub_dir.rglob("*"):
        if len(all_paths) >= _PUB_SCAN_MAX:
            truncated = True
            break
        all_paths.append(p)
    if truncated:
        log.warning("pub/ for session %r has >%d entries, listing truncated", session, _PUB_SCAN_MAX)
    # prefer external_url setting over request-derived base_url
    external_url = config.settings.get("external_url", "")
    if external_url:
        prefix = external_url.rstrip("/")
    else:
        prefix = base_url.rstrip("/") if base_url else ""
    results = []
    for f in sorted(all_paths):
        if f.is_file():
            rel = f.relative_to(pub_dir)
            results.append({
                "filename": str(rel),
                "url": f"{prefix}/pub/{token}/{rel}",
            })
    return results


_PUB_FILES_MARKER = "Published files:\n"


def _format_pub_note(pub_urls: list[dict]) -> str:
    """Format published file URLs as an output appendix."""
    if not pub_urls:
        return ""
    return "\n\n" + _PUB_FILES_MARKER + "\n".join(
        f"- {u['filename']}: {u['url']}" for u in pub_urls
    )


def _snapshot_workspace(session: str) -> set[Path]:
    """Return the set of file paths currently in the session workspace."""
    workspace = _session_workspace(session)
    return set(workspace.rglob("*"))


# Top-level directories in the workspace that should never be auto-published.
# Skills/tools create caches, profiles, and temp files here — they are internal,
# not user-facing output.  Skills can still write directly to pub/ if they want
# to publish specific files.
_PUB_IGNORE_DIRS = frozenset({
    ".browser", ".cache", ".local", ".config", ".mozilla", ".playwright",
    "__pycache__", "node_modules", ".npm", ".yarn", ".pnpm",
    ".git", ".venv", ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
})


def _auto_publish_skill_files(
    session: str, pre_snapshot: set[Path],
) -> list[str]:
    """Copy new workspace files (created after *pre_snapshot*) into pub/.

    Skips directories and files already inside pub/, files under ignored
    directories (caches, profiles, etc.), and hidden dotfiles. Returns
    list of published filenames.
    """
    workspace = _session_workspace(session)
    pub_dir = workspace / "pub"
    pub_dir.mkdir(exist_ok=True)

    new_files = set(workspace.rglob("*")) - pre_snapshot
    published: list[str] = []
    for f in sorted(new_files):
        if not f.is_file():
            continue
        # Skip files already in pub/
        try:
            f.relative_to(pub_dir)
            continue
        except ValueError:
            pass
        rel = f.relative_to(workspace)
        # Skip files under ignored directories
        if rel.parts[0] in _PUB_IGNORE_DIRS:
            continue
        # Skip hidden dotfiles (but not dotdirs already handled above)
        if any(p.startswith(".") for p in rel.parts):
            continue
        dest = pub_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        published.append(str(rel))
        log.debug("Auto-published %s → %s", f, dest)
    return published


# ── Session file listing ──────────────────────────────────────────────────

_FILE_TYPE_MAP: dict[str, str] = {}
for _ext, _cat in [
    (".png", "image"), (".jpg", "image"), (".jpeg", "image"),
    (".webp", "image"), (".gif", "image"), (".bmp", "image"), (".svg", "image"),
    (".pdf", "document"), (".docx", "document"), (".xlsx", "document"),
    (".csv", "document"), (".txt", "document"), (".md", "document"),
    (".html", "document"),
    (".mp3", "audio"), (".wav", "audio"), (".ogg", "audio"),
    (".flac", "audio"), (".m4a", "audio"),
    (".py", "code"), (".js", "code"), (".ts", "code"), (".go", "code"),
    (".rs", "code"), (".sh", "code"), (".c", "code"), (".java", "code"),
]:
    _FILE_TYPE_MAP[_ext] = _cat

_SESSION_FILES_CAP = 20


def _human_size(nbytes: int) -> str:
    """Format bytes as human-readable size."""
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f} KB"
    return f"{nbytes / (1024 * 1024):.1f} MB"


def _human_age(seconds: float) -> str:
    """Format age in seconds as human-readable relative time."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = int(seconds / 60)
        return f"{m} min ago"
    if seconds < 86400:
        h = int(seconds / 3600)
        return f"{h} h ago"
    d = int(seconds / 86400)
    return f"{d} d ago"


def _list_session_files(session: str) -> str:
    """List files in the session workspace for planner context.

    Scans the workspace, excludes .kiso/, _PUB_IGNORE_DIRS, and hidden
    dotfiles. Returns a formatted string or empty string if no files.
    """
    import time

    workspace = _session_workspace(session)
    now = time.time()
    entries: list[tuple[float, str]] = []  # (mtime, formatted_line)

    for f in workspace.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(workspace)
        parts = rel.parts
        # Skip .kiso/ directory
        if parts[0] == ".kiso":
            continue
        # Skip _PUB_IGNORE_DIRS
        if parts[0] in _PUB_IGNORE_DIRS:
            continue
        # Skip hidden dotfiles/dotdirs
        if any(p.startswith(".") for p in parts):
            continue

        stat = f.stat()
        size = _human_size(stat.st_size)
        age = _human_age(now - stat.st_mtime)
        ext = f.suffix.lower()
        category = _FILE_TYPE_MAP.get(ext, "other")
        entries.append((stat.st_mtime, f"- {rel} ({size}, {category}, {age})"))

    if not entries:
        return ""

    # Sort by mtime descending, cap at 20
    entries.sort(key=lambda e: e[0], reverse=True)
    lines = [e[1] for e in entries[:_SESSION_FILES_CAP]]
    return "Session workspace files:\n" + "\n".join(lines)


_LAST_PLAN_SUMMARY = ".kiso/last_plan_summary.json"
_PLAN_SUMMARY_MAX_AGE = 30 * 60  # 30 minutes


def _write_last_plan_summary(
    session: str,
    goal: str,
    completed_tasks: list[dict],
    pre_snapshot: set[Path],
) -> None:
    """Persist a compact summary of the completed plan for cross-plan context."""
    import datetime

    workspace = _session_workspace(session)
    post_snapshot = set(workspace.rglob("*"))
    new_files = post_snapshot - pre_snapshot

    # Produced files — only real files, skip ignored dirs and dotfiles
    produced_files: list[dict] = []
    for f in sorted(new_files):
        if not f.is_file():
            continue
        rel = f.relative_to(workspace)
        parts = rel.parts
        if parts[0] == ".kiso" or parts[0] in _PUB_IGNORE_DIRS:
            continue
        if any(p.startswith(".") for p in parts):
            continue
        # Find source tool from completed tasks
        source_tool = None
        for t in completed_tasks:
            if t.get("skill") or t.get("tool"):
                source_tool = t.get("skill") or t.get("tool")
        ext = f.suffix.lower()
        category = _FILE_TYPE_MAP.get(ext, "other")
        produced_files.append({
            "path": str(rel),
            "tool": source_tool,
            "type": category,
        })

    # Key results — reviewer summaries from completed tasks
    key_results: list[str] = []
    for t in completed_tasks:
        summary = t.get("reviewer_summary", "")
        if summary:
            key_results.append(summary[:200])
        if len(key_results) >= 3:
            break

    data = {
        "goal": goal,
        "produced_files": produced_files[:10],
        "key_results": key_results,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    # Enforce budget: truncate JSON to ~2000 chars
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw) > 2000:
        data["key_results"] = [r[:100] for r in key_results[:2]]
        data["produced_files"] = produced_files[:5]

    kiso_dir = workspace / ".kiso"
    kiso_dir.mkdir(exist_ok=True)
    path = kiso_dir / "last_plan_summary.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_last_plan_summary(session: str) -> str | None:
    """Load the last plan summary if fresh (< 30 min). Returns formatted text or None."""
    import time

    workspace = _session_workspace(session)
    path = workspace / _LAST_PLAN_SUMMARY
    if not path.is_file():
        return None

    try:
        stat = path.stat()
        if time.time() - stat.st_mtime > _PLAN_SUMMARY_MAX_AGE:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    goal = data.get("goal", "unknown")
    parts = [f"Last plan: {goal}"]

    files = data.get("produced_files", [])
    if files:
        file_strs = [f"{f['path']} ({f.get('type', 'other')})" for f in files]
        parts.append(f"Produced: {', '.join(file_strs)}")

    results = data.get("key_results", [])
    if results:
        parts.append("Results: " + "; ".join(results))

    return "\n".join(parts)


# output budget constants
_PLAN_OUTPUTS_BUDGET = 8000      # max total chars for plan_outputs in LLM context
_REPLAN_OUTPUT_LIMIT = 1000      # chars per exec/tool task output
_REPLAN_SEARCH_OUTPUT_LIMIT = 2000  # chars per search task output
_REPLAN_CONTEXT_CHAR_BUDGET = 20000  # total replan context budget (~5000 tokens)
_LARGE_OUTPUT_THRESHOLD = 4096   # chars — above this, save to file
_LARGE_OUTPUT_HEAD = 500         # chars to keep inline as preview
_PUB_SCAN_MAX = 1000             # max pub/ entries to scan


def _extract_published_urls(plan_outputs: list[dict]) -> list[str]:
    """Extract published file URL lines from plan outputs.

    Scans raw output (not reviewer summary) for the ``Published files:``
    block appended by ``_format_pub_note`` and returns the ``- file: url``
    lines.  These are surfaced prominently so the messenger never has to
    guess URLs from truncated or summarized output.
    """
    marker = _PUB_FILES_MARKER
    marker_len = len(marker)
    lines: list[str] = []
    for entry in plan_outputs:
        raw = entry.get("output") or ""
        pos = raw.find(marker)
        if pos < 0:
            continue
        for line in raw[pos + marker_len:].splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and "://" in stripped:
                lines.append(stripped)
            elif not stripped:
                break  # blank line ends the block
    return lines


def _format_plan_outputs_for_msg(
    plan_outputs: list[dict], budget: int = _PLAN_OUTPUTS_BUDGET,
) -> str:
    """Format plan_outputs as readable text for the worker LLM prompt.

    Processes outputs in reverse order (most recent first) so the messenger
    and exec translator always see the freshest context.  Once the budget
    is exhausted, older entries are reduced to one-line summaries.

    Published file URLs are extracted and placed in a prominent header
    section so the messenger always has exact URLs available, even
    when task outputs are truncated or summarized.
    """
    if not plan_outputs:
        return ""

    # collect published file URLs before any truncation
    pub_urls = _extract_published_urls(plan_outputs)

    # Build full entries in reverse, track budget
    full_parts: list[tuple[int, str]] = []  # (original_index, text)
    summary_parts: list[tuple[int, str]] = []
    budget_used = 0

    for entry in reversed(plan_outputs):
        idx = entry["index"]
        header = f"[{idx}] {entry['type']}: {entry['detail']}"
        status = entry["status"]
        # Prefer reviewer summary over raw output when available
        reviewer_summary = entry.get("reviewer_summary")
        if reviewer_summary:
            output = f"Summary: {reviewer_summary}"
        else:
            output = entry.get("output") or "(no output)"
        full_text = f"{header}\nStatus: {status}\n{fence_content(output, 'TASK_OUTPUT')}"

        if budget_used + len(full_text) <= budget:
            full_parts.append((idx, full_text))
            budget_used += len(full_text)
        else:
            summary_parts.append((idx, f"[{idx}] {entry['type']}: {entry['detail']} -> {status}"))

    # Re-sort by original index (ascending)
    full_parts.sort(key=lambda x: x[0])
    summary_parts.sort(key=lambda x: x[0])

    parts: list[str] = []
    # published file URLs at the top so messenger never misses them
    if pub_urls:
        parts.append("## Published Files\n" + "\n".join(pub_urls))
    if summary_parts:
        parts.append("(earlier tasks summarized)\n" + "\n".join(t for _, t in summary_parts))
    parts.extend(t for _, t in full_parts)
    return "\n\n".join(parts)



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


def _task_type_label(t: dict) -> str:
    """Format task type, including tool name when present (e.g. 'tool/ocr')."""
    label = t["type"]
    if t.get("tool"):
        label += f"/{t['tool']}"
    return label


def _format_task_list(tasks: list[dict], label: str) -> str:
    """Format a task list with label and count, e.g. 'Completed (3):\\n- [exec] ...'."""
    if not tasks:
        return ""
    items = [f"- [{_task_type_label(t)}] {t['detail']}" for t in tasks]
    return f"{label} ({len(tasks)}):\n" + "\n".join(items)


def _smart_truncate(text: str, limit: int) -> str:
    """Truncate *text* to *limit* chars, cutting at a newline boundary."""
    if len(text) <= limit:
        return text
    # Find last newline within limit
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut] + "\n... (truncated)"


_FACT_LINE_LIMIT = 20       # max lines scanned for install/error keywords
_FACT_CHAR_LIMIT = 200      # max chars per extracted fact line
_FACT_TOTAL_CAP = 15        # max facts returned


def _facts_from_summaries(completed: list[dict], seen: set[str]) -> list[str]:
    """Extract reviewer summaries (highest priority)."""
    facts: list[str] = []
    for t in completed:
        summary = t.get("reviewer_summary")
        if summary and summary not in seen:
            facts.append(summary)
            seen.add(summary)
    return facts


def _facts_from_registry(output: str, seen: set[str]) -> list[str] | None:
    """Parse JSON registry responses. Returns facts or None if not JSON."""
    if output[:1] not in ("{", "["):
        return None
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "name" in data:
            fact = f"Skill/connector '{data['name']}' found in registry"
            if "version" in data:
                fact += f" (v{data['version']})"
            if fact not in seen:
                seen.add(fact)
                return [fact]
            return []
        if isinstance(data, list):
            names = [item.get("name") for item in data if isinstance(item, dict) and "name" in item]
            if names:
                fact = f"Registry contains: {', '.join(names[:10])}"
                if fact not in seen:
                    seen.add(fact)
                    return [fact]
                return []
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None


def _facts_from_output_lines(output: str, seen: set[str]) -> list[str]:
    """Scan for install/error keywords or extract first line."""
    facts: list[str] = []
    for line in output.split("\n")[:_FACT_LINE_LIMIT]:
        line_lower = line.strip().lower()
        if not line_lower:
            continue
        if any(kw in line_lower for kw in ("installed", "available", "not found", "error")):
            fact = line.strip()[:_FACT_CHAR_LIMIT]
            if fact not in seen:
                facts.append(fact)
                seen.add(fact)
            break
    return facts


def _extract_confirmed_facts(completed: list[dict]) -> list[str]:
    """Best-effort extraction of confirmed facts from completed task outputs.

    Strategies (in priority order):
    1. Reviewer summaries from successful tasks
    2. JSON registry responses (name/version)
    3. Lines with install/error keywords
    4. First line of short outputs
    """
    seen: set[str] = set()
    facts: list[str] = _facts_from_summaries(completed, seen)

    for t in completed:
        out = (t.get("output") or "").strip()
        if not out:
            continue

        # try JSON registry parse first
        registry_facts = _facts_from_registry(out, seen)
        if registry_facts is not None:
            facts.extend(registry_facts)
            continue

        # Keyword-based extraction
        facts.extend(_facts_from_output_lines(out, seen))

        # Short output fallback: first line as fact
        if out[:_FACT_CHAR_LIMIT] not in seen and len(out) < _FACT_CHAR_LIMIT and t.get("status") == "done":
            first_line = out.split("\n")[0].strip()[:_FACT_CHAR_LIMIT]
            if first_line and first_line not in seen:
                facts.append(first_line)
                seen.add(first_line)

    return facts[:_FACT_TOTAL_CAP]


def _format_replan_hints(
    update_hints: list[str] | None,
    replan_history: list[dict],
) -> list[str]:
    """Build User Updates and Suggested Fixes sections."""
    parts: list[str] = []
    if update_hints:
        bullets = "\n".join(f"- {h}" for h in update_hints)
        parts.append(
            "## User Updates (received during execution — apply these changes)\n"
            + bullets
        )
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
    return parts


def _format_replan_facts(
    completed: list[dict],
    replan_history: list[dict],
) -> str | None:
    """Build Confirmed Facts section from completed tasks + history."""
    all_completed = list(completed)
    for h in replan_history:
        for ko in h.get("key_outputs", []):
            if ko.startswith("[") and "] " in ko:
                out_text = ko[ko.index("] ") + 2:]
                all_completed.append({"type": "exec", "output": out_text, "status": "done"})
    confirmed = _extract_confirmed_facts(all_completed)
    if not confirmed:
        return None
    bullets = "\n".join(f"- {f}" for f in confirmed)
    return (
        "## Confirmed Facts (DO NOT re-verify these — they are already established)\n"
        + bullets
    )


def _format_replan_tasks(
    completed: list[dict],
    remaining: list[dict],
) -> list[str]:
    """Build Completed Tasks and Remaining Tasks sections with budget tracking."""
    parts: list[str] = []
    if completed:
        items = []
        total_chars = 0
        for t in completed:
            limit = _REPLAN_SEARCH_OUTPUT_LIMIT if t.get("type") == "search" else _REPLAN_OUTPUT_LIMIT
            if total_chars >= _REPLAN_CONTEXT_CHAR_BUDGET:
                items.append(f"- [{_task_type_label(t)}] {t['detail']}: {t['status']}")
                continue
            reviewer_summary = t.get("reviewer_summary")
            if reviewer_summary:
                out_fenced = f"Summary: {reviewer_summary}"
            else:
                raw_out = t.get("output") or ""
                out = _smart_truncate(raw_out, limit)
                out_fenced = fence_content(out, "TASK_OUTPUT") if out else "(no output)"
            item = f"- [{_task_type_label(t)}] {t['detail']}: {t['status']} →\n{out_fenced}"
            items.append(item)
            total_chars += len(item)
        parts.append("## Completed Tasks\n" + "\n".join(items))
    if remaining:
        items = [f"- [{_task_type_label(t)}] {t['detail']}" for t in remaining]
        parts.append("## Remaining Tasks (not executed)\n" + "\n".join(items))
    return parts


_HISTORY_OUTPUT_BUDGET = 3000  # max chars for all key_outputs across history


def _format_replan_history(replan_history: list[dict]) -> str | None:
    """Build Previous Replan Attempts section with output budget."""
    if not replan_history:
        return None
    items = []
    output_chars = 0
    for h in replan_history:
        tried = ", ".join(h.get("what_was_tried", [])) or "nothing"
        entry = f"- Goal: {h['goal']}, Tried: {tried}, Failure: {h['failure']}"
        for hint in h.get("retry_hints", []):
            entry += f"\n  Reviewer hint: {hint}"
        if h.get("no_retry_count") and not h.get("retry_hints"):
            entry += "\n  Note: reviewer indicated no retry possible — try an alternative approach or explain to user."
        for summary in h.get("reviewer_summaries", [])[:2]:
            entry += f"\n  Reviewer summary: {summary[:300]}"
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
    return (
        "## Previous Replan Attempts (DO NOT repeat these approaches)\n"
        + "\n".join(items)
    )


def _build_replan_context(
    completed: list[dict],
    remaining: list[dict],
    replan_reason: str,
    replan_history: list[dict],
    update_hints: list[str] | None = None,
) -> str:
    """Build extra context for replanning."""
    # strip msg-type tasks — intent messages are noise for replanning
    completed = [t for t in completed if t.get("type") != "msg"]
    parts: list[str] = []
    parts.extend(_format_replan_hints(update_hints, replan_history))
    facts_section = _format_replan_facts(completed, replan_history)
    if facts_section:
        parts.append(facts_section)
    parts.extend(_format_replan_tasks(completed, remaining))
    parts.append(f"## Failure Reason\n{replan_reason}")
    history_section = _format_replan_history(replan_history)
    if history_section:
        parts.append(history_section)
    return "\n\n".join(parts)


def _build_cancel_summary(
    completed: list[dict], remaining: list[dict], goal: str,
) -> str:
    """Build a detail string for the worker LLM summarising a cancel."""
    parts: list[str] = [f"The user cancelled the plan: {goal}"]

    completed_text = _format_task_list(completed, "Completed")
    parts.append(completed_text or "No tasks were completed.")

    skipped_text = _format_task_list(remaining, "Skipped")
    if skipped_text:
        parts.append(skipped_text)

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

    completed_text = _format_task_list(completed, "Completed successfully")
    parts.append(completed_text or "No tasks were completed.")

    # when all tasks succeeded but replanning failed, make it explicit
    if completed and not remaining:
        parts.append(
            "All planned tasks completed successfully. "
            "The failure occurred during re-planning for the next phase."
        )

    failed_text = _format_task_list(remaining, "Failed/Skipped")
    if failed_text:
        parts.append(failed_text)

    parts.append(
        "Generate a brief message explaining what went wrong and suggest "
        "next steps. Completed tasks SUCCEEDED — do NOT say they failed. "
        "Focus the error on the failure reason only."
    )
    return "\n\n".join(parts)


# ── Replan notification messages ──

_REPLAN_TEMPLATES: dict[str, str] = {
    "investigating": "Investigating... ({depth}/{max})",
    "replanning": "Replanning (attempt {depth}/{max}): {reason}",
    "stuck": (
        "I'm having trouble with this request. "
        "I've tried replanning {depth} times but keep hitting "
        "the same issue: {reason}\n"
        "Previous attempts: {tried}\n"
        "Can you help me with more details or a different approach?"
    ),
}


def get_replan_message(
    kind: str,
    depth: int,
    max_depth: int,
    reason: str = "",
    tried: str = "",
) -> str:
    """Get a replan notification message."""
    template = _REPLAN_TEMPLATES[kind]
    return template.format(depth=depth, max=max_depth, reason=reason, tried=tried)
