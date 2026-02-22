"""System environment context for the planner LLM.

Collects OS info, available binaries, connector status, and kiso
configuration into a concise text block injected into planner context.

Cached in-memory with TTL + explicit invalidation.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import time
from pathlib import Path

from kiso.config import Config, KISO_DIR

log = logging.getLogger(__name__)

PROBE_BINARIES: list[str] = [
    "git", "python3", "python", "uv", "pip",
    "node", "npm", "npx",
    "docker", "docker-compose",
    "curl", "wget", "ssh", "rsync",
    "jq", "yq",
    "ffmpeg", "imagemagick",
    "tar", "gzip", "zip", "unzip",
    "grep", "sed", "awk", "find", "xargs",
    "make", "gcc", "go", "rustc", "cargo",
]

_CACHE_TTL = 300  # seconds

# Module-level cache
_cached_env: dict | None = None
_cached_at: float = 0.0


def _collect_os_info() -> dict[str, str]:
    """Collect OS platform info."""
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "release": platform.release(),
    }


def _collect_binaries(
    probe_list: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Probe binaries with shutil.which(). Returns (found, missing).

    Prepends ``sys/bin/`` to PATH so that binaries installed by
    ``deps.sh`` into the persistent directory are discovered.
    """
    probe = probe_list if probe_list is not None else PROBE_BINARIES
    # Prepend sys/bin to PATH for probing
    sys_bin = str(KISO_DIR / "sys" / "bin")
    orig_path = os.environ.get("PATH", "/usr/bin:/bin")
    extended_path = f"{sys_bin}:{orig_path}" if os.path.isdir(sys_bin) else orig_path
    found: list[str] = []
    missing: list[str] = []
    for name in probe:
        if shutil.which(name, path=extended_path):
            found.append(name)
        else:
            missing.append(name)
    return found, missing


def _collect_connectors() -> list[dict[str, str]]:
    """Discover connectors and check running status via PID files."""
    # Lazy import to avoid circular deps
    from kiso.cli_connector import discover_connectors

    connectors = discover_connectors()
    result: list[dict[str, str]] = []
    for c in connectors:
        from pathlib import Path

        connector_dir = Path(c["path"])
        pid_file = connector_dir / ".pid"
        status = "stopped"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                status = "running"
            except (ValueError, ProcessLookupError, OSError):
                status = "stopped"
        result.append({
            "name": c["name"],
            "platform": c.get("platform", ""),
            "status": status,
        })
    return result


def collect_system_env(config: Config) -> dict:
    """Assemble all system environment info into one dict."""
    os_info = _collect_os_info()
    found_bins, missing_bins = _collect_binaries()
    connectors = _collect_connectors()

    return {
        "os": os_info,
        "shell": "/bin/sh",
        "exec_cwd": str(KISO_DIR / "sessions"),
        "exec_env": "PATH (sys/bin prepended) + HOME + git/ssh env vars when config exists",
        "exec_timeout": int(config.settings.get("exec_timeout", 120)),
        "max_output_size": int(config.settings.get("max_output_size", 1_048_576)),
        "available_binaries": found_bins,
        "missing_binaries": missing_bins,
        "connectors": connectors,
        "max_plan_tasks": int(config.settings.get("max_plan_tasks", 20)),
        "max_replan_depth": int(config.settings.get("max_replan_depth", 3)),
        "sys_bin_path": str(KISO_DIR / "sys" / "bin"),
        "reference_docs_path": str(KISO_DIR / "reference"),
        "registry_url": "https://raw.githubusercontent.com/kiso-run/core/main/registry.json",
    }


def get_system_env(config: Config) -> dict:
    """Return cached system env or re-collect if stale/invalidated."""
    global _cached_env, _cached_at
    now = time.monotonic()
    if _cached_env is not None and (now - _cached_at) < _CACHE_TTL:
        return _cached_env
    _cached_env = collect_system_env(config)
    _cached_at = now
    return _cached_env


def invalidate_cache() -> None:
    """Clear the module-level cache. Forces re-collection on next call."""
    global _cached_env, _cached_at
    _cached_env = None
    _cached_at = 0.0


_KISO_CLI_COMMANDS = """\
  kiso skill list | search [query] | install <name|url> | update <name|all> | remove <name>
  kiso connector list | search [query] | install <name|url> | update <name|all> | remove <name>
  kiso connector run <name> | stop <name> | status <name>
  kiso env set <KEY> <VALUE> | get <KEY> | list | delete <KEY> | reload"""

_BLOCKED_COMMANDS = (
    "rm -rf / ~ $HOME, dd if=, mkfs, chmod -R 777 /, "
    "recursive chown, shutdown/reboot, fork bombs"
)


def _format_size(nbytes: int) -> str:
    """Format byte count as human-readable (e.g. 1048576 -> '1MB')."""
    if nbytes >= 1_048_576 and nbytes % 1_048_576 == 0:
        return f"{nbytes // 1_048_576}MB"
    if nbytes >= 1024 and nbytes % 1024 == 0:
        return f"{nbytes // 1024}KB"
    return f"{nbytes}B"


def _collect_workspace_files(session: str) -> str:
    """List files in the session workspace (excluding .kiso/ internals).

    Returns a compact listing: relative path + human size, max 30 entries.
    """
    workspace = KISO_DIR / "sessions" / session
    if not workspace.is_dir():
        return ""

    entries: list[str] = []
    for f in sorted(workspace.rglob("*")):
        if f.is_file() and ".kiso" not in f.relative_to(workspace).parts:
            rel = f.relative_to(workspace)
            size = _format_size(f.stat().st_size)
            entries.append(f"{rel} ({size})")
        if len(entries) >= 30:
            entries.append("... (truncated, use `find` for full listing)")
            break
    return ", ".join(entries)


def build_system_env_section(env: dict, session: str = "") -> str:
    """Format the system env dict into a concise text block for the planner.

    When *session* is provided the ``Exec CWD`` line shows the actual
    absolute workspace path and a ``Session`` line is added.
    """
    os_info = env["os"]
    lines: list[str] = []

    lines.append(
        f"OS: {os_info['system']} {os_info['machine']} ({os_info['release']})"
    )
    lines.append(f"Shell: {env['shell']}")
    if session:
        cwd = str(KISO_DIR / "sessions" / session)
        lines.append(f"Session: {session}")
    else:
        cwd = env["exec_cwd"] + "/<session>/"
    lines.append(f"Exec CWD: {cwd}")
    lines.append("Network: outbound internet access available (use `curl` for HTTP requests, `wget` for downloads)")
    lines.append("Public files: write to pub/ in exec CWD → auto-served at /pub/ URLs (no auth needed)")
    if session:
        ws_files = _collect_workspace_files(session)
        if ws_files:
            lines.append(f"Workspace files: {ws_files}")
        else:
            lines.append("Workspace files: (empty)")
        lines.append("File search: use `find` (by name/date/size), `grep`/`rg` (by content), `file` (by type) in exec tasks")
    lines.append(f"Exec env: {env['exec_env']}")
    lines.append(f"Persistent dir: ~/.kiso/sys/ (git config, ssh keys, runtime binaries)")
    lines.append(f"Sys bin: {env['sys_bin_path']} (prepended to exec PATH)")
    lines.append(f"Reference docs: {env['reference_docs_path']} (skill/connector authoring guides — cat before planning)")
    lines.append(f"Plugin registry: {env['registry_url']} (curl to discover available skills/connectors)")
    lines.append(
        f"Exec timeout: {env['exec_timeout']}s | "
        f"Max output: {_format_size(env['max_output_size'])}"
    )
    lines.append("")

    if env["available_binaries"]:
        lines.append(f"Available binaries: {', '.join(env['available_binaries'])}")
    if env["missing_binaries"]:
        lines.append(f"Missing common tools: {', '.join(env['missing_binaries'])}")
    lines.append("")

    connectors = env["connectors"]
    if connectors:
        parts = [f"{c['name']} ({c['status']})" for c in connectors]
        lines.append(f"Connectors: {', '.join(parts)}")
    else:
        lines.append("Connectors: none installed")
    lines.append("")

    lines.append("Kiso CLI (usable in exec tasks):")
    lines.append(_KISO_CLI_COMMANDS)
    lines.append("")

    lines.append(f"Blocked commands: {_BLOCKED_COMMANDS}")
    lines.append(
        f"Plan limits: max {env['max_plan_tasks']} tasks per plan, "
        f"max {env['max_replan_depth']} replans (extendable by planner up to +3)"
    )

    return "\n".join(lines)
