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
from dataclasses import dataclass, field
import datetime
from pathlib import Path

from kiso.config import KISO_DIR, Config
from kiso.pub import pub_token
from kiso.security import fence_content
from kiso.worker.replan import (
    _build_cancel_summary,
    _build_failure_summary,
    _build_replan_context,
    _extract_confirmed_facts,
    _extract_published_urls,
    _format_plan_outputs_for_msg,
    _format_task_list,
    _smart_truncate,
    get_replan_message,
)
from kiso.worker.state import (
    ArtifactRef,
    FileRef,
    TaskContract,
    TaskResult,
    _coerce_task_args,
    _coerce_task_contract,
    _collect_task_results,
    _normalize_task_contract,
    _task_result_from_source,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionState:
    """Canonical minimal runtime state for a session workspace.

    This is intentionally small and built from the runtime artifacts Kiso
    already persists today. Prompt-facing text sections are rendered from
    this state rather than assembled independently in multiple places.
    """

    session: str
    workspace_root: str
    workspace_files: list[dict] = field(default_factory=list)
    last_plan_goal: str | None = None
    last_plan_produced_files: list[dict] = field(default_factory=list)
    last_plan_key_results: list[str] = field(default_factory=list)
    last_plan_ts: str | None = None

    def context_sections(self) -> dict[str, str]:
        """Render prompt-facing sections from the canonical state."""
        sections: dict[str, str] = {}
        session_files_text = _format_workspace_files(self.workspace_files)
        if session_files_text:
            sections["session_files"] = session_files_text
        last_plan_text = _format_last_plan_summary_data(
            {
                "goal": self.last_plan_goal,
                "produced_files": self.last_plan_produced_files,
                "key_results": self.last_plan_key_results,
                "ts": self.last_plan_ts,
            },
        )
        if last_plan_text:
            sections["last_plan"] = last_plan_text
        return sections




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


def _path_type(path: Path) -> str:
    """Return the canonical file category for a path."""
    return _FILE_TYPE_MAP.get(path.suffix.lower(), "other")


def _module_name_for_path(path: Path) -> str | None:
    """Return importable module name for a plain Python file, if any."""
    if path.suffix.lower() != ".py":
        return None
    stem = path.stem
    return stem if stem.isidentifier() else None


def _make_file_ref(
    path: Path | str,
    *,
    workspace: Path | None = None,
    origin_task_index: int | None = None,
    origin_wrapper: str | None = None,
) -> FileRef:
    """Create a canonical FileRef from an absolute or relative path."""
    file_path = Path(path)
    if not file_path.is_absolute():
        if workspace is None:
            raise ValueError("workspace is required for relative file refs")
        file_path = (workspace / file_path).resolve()
    workspace_path: str | None = None
    if workspace is not None:
        try:
            workspace_path = str(file_path.relative_to(workspace))
        except ValueError:
            workspace_path = None
    ref_key = workspace_path or str(file_path)
    return FileRef(
        file_id=f"file:{ref_key}",
        abs_path=str(file_path),
        workspace_path=workspace_path,
        type=_path_type(file_path),
        exists=file_path.exists(),
        module_name=_module_name_for_path(file_path),
        origin_task_index=origin_task_index,
        origin_wrapper=origin_wrapper,
    )


def _make_artifact_ref(
    path: Path | str,
    *,
    workspace: Path,
    origin_task_index: int | None = None,
    origin_wrapper: str | None = None,
) -> ArtifactRef:
    """Create a file-backed ArtifactRef."""
    file_ref = _make_file_ref(
        path,
        workspace=workspace,
        origin_task_index=origin_task_index,
        origin_wrapper=origin_wrapper,
    )
    artifact_key = file_ref.workspace_path or file_ref.abs_path
    return ArtifactRef(
        artifact_id=f"artifact:{artifact_key}",
        kind="file",
        file_ref=file_ref,
    )


def _collect_workspace_files(session: str) -> list[dict]:
    """Collect visible workspace files as structured records."""
    import time

    workspace = _session_workspace(session)
    now = time.time()
    entries: list[tuple[float, dict]] = []

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
        ext = f.suffix.lower()
        category = _FILE_TYPE_MAP.get(ext, "other")
        entries.append((
            stat.st_mtime,
            {
                "file_id": _make_file_ref(f, workspace=workspace).file_id,
                "path": str(rel),
                "workspace_path": str(rel),
                "abs_path": str(f),
                "size_bytes": stat.st_size,
                "size_human": _human_size(stat.st_size),
                "age_human": _human_age(now - stat.st_mtime),
                "mtime": stat.st_mtime,
                "type": category,
                "exists": True,
                "module_name": _module_name_for_path(f),
            },
        ))

    if not entries:
        return []

    # Sort by mtime descending, cap at 20
    entries.sort(key=lambda e: e[0], reverse=True)
    return [e[1] for e in entries[:_SESSION_FILES_CAP]]


def _format_workspace_files(files: list[dict]) -> str:
    """Render workspace files for planner context."""
    if not files:
        return ""
    lines = [
        f"- {entry['path']} | abs: {entry['abs_path']} "
        f"({entry['size_human']}, {entry['type']}, {entry['age_human']})"
        for entry in files
    ]
    return "Session workspace files:\n" + "\n".join(lines)


def _build_provenance_index(
    plan_outputs: list[dict] | None,
) -> dict[str, tuple[str | None, int | None]]:
    """Map workspace-relative path → (origin_wrapper, origin_task_index).

    Walks each plan_output's ``file_refs`` and ``artifact_refs`` and pulls
    the (tool, index) declared on each ref. Refs without a usable path
    are skipped. When two refs target the same path, the first wins
    (consistent with append-order semantics of plan_outputs).
    """
    index: dict[str, tuple[str | None, int | None]] = {}
    for output in plan_outputs or []:
        for ref_list_key in ("file_refs", "artifact_refs"):
            for ref in output.get(ref_list_key) or []:
                if not isinstance(ref, dict):
                    continue
                key = ref.get("workspace_path") or ref.get("path") or ref.get("abs_path")
                if not key or key in index:
                    continue
                index[key] = (
                    ref.get("origin_wrapper"),
                    ref.get("origin_task_index"),
                )
    return index


def _build_last_plan_summary_data(
    session: str,
    goal: str,
    completed_tasks: list[dict],
    pre_snapshot: set[Path],
    plan_outputs: list[dict] | None = None,
) -> dict:
    """Build the persisted last-plan summary payload."""
    workspace = _session_workspace(session)
    post_snapshot = set(workspace.rglob("*"))
    new_files = post_snapshot - pre_snapshot

    # Per-file provenance derived from plan_output file/artifact refs.
    # If a file has no matching ref, provenance stays None — we never
    # invent it by scanning unrelated completed tasks.
    provenance = _build_provenance_index(plan_outputs)

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
        rel_str = str(rel)
        origin_wrapper, origin_task_index = provenance.get(
            rel_str, provenance.get(str(f), (None, None))
        )
        artifact = _make_artifact_ref(
            f,
            workspace=workspace,
            origin_task_index=origin_task_index,
            origin_wrapper=origin_wrapper,
        ).to_dict()
        artifact["tool"] = origin_wrapper
        produced_files.append(artifact)

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
    return data


_LAST_PLAN_SUMMARY = ".kiso/last_plan_summary.json"
_PLAN_SUMMARY_MAX_AGE = 30 * 60  # 30 minutes


def _write_last_plan_summary(
    session: str,
    goal: str,
    completed_tasks: list[dict],
    pre_snapshot: set[Path],
    plan_outputs: list[dict] | None = None,
) -> None:
    """Persist a compact summary of the completed plan for cross-plan context."""
    workspace = _session_workspace(session)
    data = _build_last_plan_summary_data(
        session, goal, completed_tasks, pre_snapshot, plan_outputs=plan_outputs,
    )

    kiso_dir = workspace / ".kiso"
    kiso_dir.mkdir(exist_ok=True)
    path = kiso_dir / "last_plan_summary.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_last_plan_summary_data(session: str) -> dict | None:
    """Load the last plan summary payload if it is still fresh."""
    import time

    workspace = _session_workspace(session)
    path = workspace / _LAST_PLAN_SUMMARY
    if not path.is_file():
        return None

    try:
        stat = path.stat()
        if time.time() - stat.st_mtime > _PLAN_SUMMARY_MAX_AGE:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _format_last_plan_summary_data(data: dict | None) -> str | None:
    """Render a persisted last-plan summary payload for prompt context."""
    if not data:
        return None
    goal = data.get("goal", "unknown")
    parts = [f"Last plan: {goal}"]

    files = data.get("produced_files", [])
    if files:
        file_strs = [
            f"{f['path']} | abs: {f.get('abs_path', f['path'])} ({f.get('type', 'other')})"
            for f in files
        ]
        parts.append(f"Produced: {', '.join(file_strs)}")

    results = data.get("key_results", [])
    if results:
        parts.append("Results: " + "; ".join(results))

    return "\n".join(parts)


def _load_last_plan_summary(session: str) -> str | None:
    """Load the last plan summary if fresh (< 30 min). Returns formatted text or None."""
    return _format_last_plan_summary_data(_load_last_plan_summary_data(session))


def _build_execution_state(session: str) -> ExecutionState:
    """Build canonical runtime state for a session from persisted artifacts."""
    workspace = _session_workspace(session)
    summary_data = _load_last_plan_summary_data(session) or {}
    normalized_produced_files: list[dict] = []
    for item in summary_data.get("produced_files", []):
        if not isinstance(item, dict):
            continue
        if item.get("file_id") and item.get("artifact_id"):
            normalized_produced_files.append(item)
            continue
        raw_path = item.get("abs_path") or item.get("workspace_path") or item.get("path")
        if not raw_path:
            normalized_produced_files.append(item)
            continue
        artifact = _make_artifact_ref(raw_path, workspace=workspace).to_dict()
        artifact["tool"] = item.get("tool")
        normalized_produced_files.append(artifact)
    return ExecutionState(
        session=session,
        workspace_root=str(workspace),
        workspace_files=_collect_workspace_files(session),
        last_plan_goal=summary_data.get("goal"),
        last_plan_produced_files=normalized_produced_files,
        last_plan_key_results=summary_data.get("key_results", []),
        last_plan_ts=summary_data.get("ts"),
    )


def _list_session_files(session: str) -> str:
    """List files in the session workspace for planner context."""
    return _format_workspace_files(_collect_workspace_files(session))


# output budget constants
_LARGE_OUTPUT_THRESHOLD = 4096   # chars — above this, save to file
_LARGE_OUTPUT_HEAD = 500         # chars to keep inline as preview
_PUB_SCAN_MAX = 1000             # max pub/ entries to scan

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
