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

from kiso.config import Config, KISO_DIR, SETTINGS_DEFAULTS, USER_FACING_SETTINGS as _USER_FACING_SETTINGS

log = logging.getLogger(__name__)

PROBE_BINARIES: list[str] = [
    "kiso",
    # Languages / package managers
    "git", "python3", "python", "uv", "pip",
    "node", "npm", "npx",
    "make", "gcc", "go", "rustc", "cargo",
    # Containers
    "docker", "docker-compose",
    # Network / transfer
    "curl", "wget", "ssh", "scp", "rsync",
    "ssh-keygen", "ssh-keyscan",
    "ping", "dig", "nslookup", "ifconfig",
    # System info
    "free", "ps", "uptime", "uname", "id", "groups",
    "lscpu", "lsblk", "mount", "df",
    "ip", "ss", "netstat", "lsb_release", "hostname",
    # Data / JSON
    "jq", "yq",
    # Media
    "ffmpeg", "imagemagick",
    # Archive
    "tar", "gzip", "zip", "unzip", "bzip2", "xz",
    # Text processing
    "grep", "sed", "awk", "find", "xargs",
    "rg", "head", "tail", "wc", "sort", "cut", "tr", "tee", "uniq", "paste",
    # Browsers
    "chromium", "chromium-browser", "lynx", "w3m", "html2text",
    # File ops
    "diff", "file", "stat", "du", "cat", "tree",
    # Process
    "kill", "pkill",
]

_CACHE_TTL = 300  # seconds

# Module-level cache
_cached_env: dict | None = None
_cached_at: float = 0.0


def get_resource_limits() -> dict:
    """Read actual resource limits from cgroups and disk usage.

    Returns a dict with keys: memory_mb, memory_used_mb, cpu_limit,
    disk_used_gb, disk_total_gb, pids_limit, pids_used.
    Values are None when the corresponding source is unavailable.
    """
    result: dict[str, int | float | None] = {
        "memory_mb": None,
        "memory_used_mb": None,
        "cpu_limit": None,
        "disk_used_gb": None,
        "disk_total_gb": None,
        "pids_limit": None,
        "pids_used": None,
    }

    # Memory limit from cgroup v2
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw != "max":
            result["memory_mb"] = int(raw) // (1024 * 1024)
    except (ValueError, OSError):
        pass

    # Memory usage
    try:
        result["memory_used_mb"] = int(Path("/sys/fs/cgroup/memory.current").read_text().strip()) // (1024 * 1024)
    except (ValueError, OSError):
        pass

    # CPU limit from cgroup v2: "quota period" e.g. "200000 100000" = 2 CPUs
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        parts = raw.split()
        if parts[0] != "max" and len(parts) == 2:
            quota, period = int(parts[0]), int(parts[1])
            result["cpu_limit"] = round(quota / period, 1)
    except (ValueError, OSError, IndexError):
        pass

    # PIDs limit from cgroup v2
    try:
        raw = Path("/sys/fs/cgroup/pids.max").read_text().strip()
        if raw != "max":
            result["pids_limit"] = int(raw)
    except (ValueError, OSError):
        pass

    # PIDs current
    try:
        result["pids_used"] = int(Path("/sys/fs/cgroup/pids.current").read_text().strip())
    except (ValueError, OSError):
        pass

    # Disk usage — KISO_DIR actual size (not whole filesystem)
    from kiso.worker.utils import _kiso_dir_bytes

    dir_bytes = _kiso_dir_bytes()
    if dir_bytes is not None:
        result["disk_used_gb"] = round(dir_bytes / (1024**3), 1)
    # Filesystem capacity (for context)
    try:
        usage = shutil.disk_usage(str(KISO_DIR))
        result["disk_total_gb"] = round(usage.total / (1024**3), 1)
    except OSError:
        pass

    return result


_PKG_MANAGER_MAP: dict[str, str] = {
    "debian": "apt",
    "ubuntu": "apt",
    "linuxmint": "apt",
    "pop": "apt",
    "raspbian": "apt",
    "fedora": "dnf",
    "rhel": "dnf",
    "centos": "dnf",
    "rocky": "dnf",
    "almalinux": "dnf",
    "alpine": "apk",
    "arch": "pacman",
    "manjaro": "pacman",
    "opensuse": "zypper",
    "sles": "zypper",
}


def _detect_pkg_manager(distro_id: str, id_like: str = "") -> str | None:
    """Deterministic mapping from distro ID to package manager name."""
    if distro_id in _PKG_MANAGER_MAP:
        return _PKG_MANAGER_MAP[distro_id]
    for parent in id_like.split():
        if parent in _PKG_MANAGER_MAP:
            return _PKG_MANAGER_MAP[parent]
    return None


def _collect_os_info() -> dict[str, str]:
    """Collect OS platform info including distro details."""
    info: dict[str, str] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "release": platform.release(),
    }
    try:
        os_release = platform.freedesktop_os_release()
        info["distro"] = os_release.get("PRETTY_NAME", "")
        distro_id = os_release.get("ID", "")
        id_like = os_release.get("ID_LIKE", "")
        info["distro_id"] = distro_id
        info["distro_id_like"] = id_like
        pkg = _detect_pkg_manager(distro_id, id_like)
        if pkg:
            info["pkg_manager"] = pkg
    except OSError:
        pass
    return info


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
    from kiso.connectors import discover_connectors

    connectors = discover_connectors()
    result: list[dict[str, str]] = []
    for c in connectors:
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


def _load_registry_hints() -> str:
    """Load brief tool/connector descriptions from the online registry."""
    from kiso.registry import fetch_registry

    data = fetch_registry()
    if not data:
        return ""
    parts: list[str] = []
    for s in data.get("tools", []):
        name = s.get("name", "")
        desc = s.get("description", "")
        if name and desc:
            parts.append(f"{name} ({desc})")
    for c in data.get("connectors", []):
        name = c.get("name", "")
        desc = c.get("description", "")
        if name and desc:
            parts.append(f"{name} ({desc})")
    return "; ".join(parts) if parts else ""


def _collect_user_info() -> dict:
    """Detect current user, root status, and sudo availability."""
    import pwd

    try:
        uid = os.getuid()
        username = pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        uid = -1
        username = os.getenv("USER", "unknown")

    return {
        "user": username,
        "is_root": uid == 0,
        "has_sudo": shutil.which("sudo") is not None,
    }


def collect_system_env(config: Config) -> dict:
    """Assemble all system environment info into one dict."""
    os_info = _collect_os_info()
    found_bins, missing_bins = _collect_binaries()
    connectors = _collect_connectors()
    registry_hints = _load_registry_hints()
    user_info = _collect_user_info()

    return {
        "os": os_info,
        "user_info": user_info,
        "shell": "/bin/sh",
        "exec_cwd": str(KISO_DIR / "sessions"),
        "exec_env": "PATH (sys/bin prepended) + HOME + git/ssh env vars when config exists",
        "max_output_size": int(config.settings["max_output_size"]),
        "available_binaries": found_bins,
        "missing_binaries": missing_bins,
        "connectors": connectors,
        "max_plan_tasks": int(config.settings["max_plan_tasks"]),
        "max_replan_depth": int(config.settings["max_replan_depth"]),
        "sys_bin_path": str(KISO_DIR / "sys" / "bin"),
        "registry_hints": registry_hints,
        "reference_docs_path": str(KISO_DIR / "reference"),
        "registry_url": "https://raw.githubusercontent.com/kiso-run/core/main/registry.json",
        "bot_persona": config.settings.get("bot_persona", ""),
        "user_settings": {
            k: config.settings.get(k, SETTINGS_DEFAULTS.get(k))
            for k in _USER_FACING_SETTINGS
        },
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
  kiso tool list | search [query] | install <name|url> | update <name|all> | remove <name>
  kiso connector list | search [query] | install <name|url> | update <name|all> | remove <name>
  kiso connector run <name> | stop <name> | status <name>
  kiso env set <KEY> <VALUE> | get <KEY> | list | delete <KEY> | reload"""

_BLOCKED_COMMANDS = (
    "rm -rf / ~ $HOME, dd if=, mkfs, chmod -R 777 /, "
    "recursive chown, shutdown/reboot, fork bombs, "
    "direct writes to ~/.kiso/.env or ~/.kiso/config.toml (use 'kiso env set' instead)"
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

    # Collect at most _MAX_SCAN candidates to avoid materialising a huge rglob.
    _MAX_SCAN = 1000
    candidates: list[Path] = []
    for f in workspace.rglob("*"):
        if f.is_file() and ".kiso" not in f.relative_to(workspace).parts:
            candidates.append(f)
        if len(candidates) >= _MAX_SCAN:
            break
    entries: list[str] = []
    for f in sorted(candidates):
        rel = f.relative_to(workspace)
        size = _format_size(f.stat().st_size)
        entries.append(f"{rel} ({size})")
        if len(entries) >= 30:
            entries.append("... (truncated, use `find` for full listing)")
            break
    return ", ".join(entries)


def build_system_env_essential(env: dict, session: str = "") -> str:
    """Minimal system env for the planner — always injected (~60 tokens).

    Contains only what the planner needs for every plan: workspace path,
    public file rule, blocked commands, and plan limits.
    """
    lines: list[str] = []
    if session:
        cwd = str(KISO_DIR / "sessions" / session)
    else:
        cwd = env["exec_cwd"] + "/<session>/"
    lines.append(f"Exec CWD: {cwd}")
    lines.append("Public files: write to pub/ in exec CWD → system auto-generates authenticated download URLs")
    lines.append(f"Blocked commands: {_BLOCKED_COMMANDS}")
    lines.append(
        f"Plan limits: max {env['max_plan_tasks']} tasks per plan, "
        f"max {env['max_replan_depth']} replans (extendable by planner up to +3)"
    )
    return "\n".join(lines)


def build_user_settings_text(env: dict) -> str:
    """Configurable settings block for the planner (~100 tokens).

    Injected only when the ``kiso_commands`` module is loaded — the planner
    doesn't need to see consolidation_enabled or context_messages for every plan.
    """
    user_settings = env.get("user_settings", {})
    if not user_settings:
        return ""
    settings_lines = [f"  {k} = {v}" for k, v in user_settings.items()]
    return "Configurable settings (kiso config set KEY VALUE):\n" + "\n".join(settings_lines)


def build_install_context(env: dict) -> str:
    """Install-critical fields for the planner (~40-80 tokens).

    Injected alongside ``sys_env_essential`` when the planner loads
    kiso_native install-decision rules but the full system environment
    is not warranted.  Contains only what the planner needs to route
    install commands correctly: distro, package manager, and available
    binaries.
    """
    lines: list[str] = []
    os_info = env.get("os", {})
    distro = os_info.get("distro")
    if distro:
        distro_id = os_info.get("distro_id", "")
        lines.append(f"Distro: {distro}" + (f" ({distro_id})" if distro_id else ""))
    pkg_manager = os_info.get("pkg_manager")
    if pkg_manager:
        lines.append(f"Package manager: {pkg_manager}")
    if env.get("available_binaries"):
        lines.append(f"Available binaries: {', '.join(env['available_binaries'])}")
    return "\n".join(lines)


def build_system_env_section(env: dict, session: str = "") -> str:
    """Full system env for the worker and install-related planner calls.

    When *session* is provided the ``Exec CWD`` line shows the actual
    absolute workspace path and a ``Session`` line is added.
    """
    os_info = env["os"]
    lines: list[str] = []

    os_line = f"OS: {os_info['system']} {os_info['machine']} ({os_info['release']})"
    distro = os_info.get("distro")
    if distro:
        os_line += f" — {distro}"
    lines.append(os_line)
    pkg_manager = os_info.get("pkg_manager")
    if pkg_manager:
        lines.append(f"Package manager: {pkg_manager}")
    user_info = env.get("user_info", {})
    if user_info:
        username = user_info.get("user", "unknown")
        is_root = user_info.get("is_root", False)
        has_sudo = user_info.get("has_sudo", False)
        if is_root:
            lines.append(f"User: {username} (sudo not needed — already running as root)")
        elif has_sudo:
            lines.append(f"User: {username} (sudo available)")
        else:
            lines.append(f"User: {username} (sudo not available)")
    lines.append(f"Shell: {env['shell']}")
    if session:
        cwd = str(KISO_DIR / "sessions" / session)
        lines.append(f"Session: {session}")
    else:
        cwd = env["exec_cwd"] + "/<session>/"
    lines.append(f"Exec CWD: {cwd}")
    lines.append("Network: outbound internet access available (use `curl` for HTTP requests, `wget` for downloads)")
    lines.append("Public files: write to pub/ in exec CWD → system auto-generates authenticated download URLs")
    if session:
        ws_files = _collect_workspace_files(session)
        if ws_files:
            lines.append(f"Workspace files: {ws_files}")
        else:
            lines.append("Workspace files: (empty)")
        lines.append("File search: use `find` (by name/date/size), `grep`/`rg` (by content), `file` (by type) in exec tasks")
    lines.append(f"Exec env: {env['exec_env']}")
    sys_dir = KISO_DIR / "sys"
    persistent_parts = ["git config", "ssh keys", "runtime binaries"]
    ssh_pub = sys_dir / "ssh" / "id_ed25519.pub"
    if ssh_pub.exists():
        persistent_parts.append(f"ssh pub key: {ssh_pub}")
    lines.append(f"Persistent dir: {sys_dir} ({', '.join(persistent_parts)})")
    lines.append(f"Sys bin: {env['sys_bin_path']} (prepended to exec PATH)")
    lines.append(f"Reference docs: {env['reference_docs_path']} (tool/connector authoring guides — cat before planning)")
    lines.append(f"Plugin registry: {env['registry_url']}")
    lines.append(f"Max output: {_format_size(env['max_output_size'])}")
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

    lines.append(f"Blocked commands: {_BLOCKED_COMMANDS}")
    lines.append(
        f"Plan limits: max {env['max_plan_tasks']} tasks per plan, "
        f"max {env['max_replan_depth']} replans (extendable by planner up to +3)"
    )

    return "\n".join(lines)
