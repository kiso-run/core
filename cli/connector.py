"""Connector management CLI commands."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from cli.render import die

from kiso.connectors import (
    CONNECTORS_DIR,
    _connector_env_var_name,
    _validate_connector_manifest,
    discover_connectors,
    invalidate_connectors_cache,
)
def check_deps(connector: dict) -> list[str]:
    """Check [kiso.deps].bin entries with ``shutil.which``."""
    deps = connector.get("deps", {})
    bins = deps.get("bin", [])
    if not isinstance(bins, list):
        return []
    return [b for b in bins if not shutil.which(b)]
from cli.plugin_ops import (
    OFFICIAL_ORG,
    _GIT_ENV,
    _check_plugin_installed,
    _list_plugins,
    _plugin_install,
    _remove_plugin,
    _render_search_results,
    _update_plugin,
    cross_type_hint,
    fetch_registry,
    is_repo_not_found,
    is_url,
    require_admin,
    search_entries,
    url_to_name,
)

OFFICIAL_PREFIX = "connector-"


def _connector_post_install(manifest: dict, connector_dir: Path, name: str) -> None:
    """Connector-specific post-install steps: env var warnings, config copy."""
    kiso_section = manifest.get("kiso", {})
    connector_section = kiso_section.get("connector", {})
    env_decl = connector_section.get("env", {})
    connector_name = kiso_section.get("name", name)
    for key, decl in env_decl.items():
        var_name = _connector_env_var_name(connector_name, key)
        if not os.environ.get(var_name):
            req = isinstance(decl, dict) and decl.get("required", False)
            req_str = "required" if req else "optional"
            desc = decl.get("description", "") if isinstance(decl, dict) else ""
            desc_part = f" — {desc}" if desc else ""
            print(f"warning: {var_name} not set ({req_str}){desc_part}")

    # Copy config.example.toml if config.toml doesn't exist
    example_config = connector_dir / "config.example.toml"
    actual_config = connector_dir / "config.toml"
    if example_config.exists() and not actual_config.exists():
        shutil.copy2(example_config, actual_config)
        print(f"note: copied config.example.toml → config.toml (edit before running)")


# Supervisor restart settings
SUPERVISOR_MAX_FAILURES = 5
SUPERVISOR_INITIAL_BACKOFF = 1.0  # seconds
SUPERVISOR_MAX_BACKOFF = 60.0  # seconds
SUPERVISOR_BACKOFF_MULTIPLIER = 2.0
SUPERVISOR_STABLE_THRESHOLD = 60.0  # seconds — if child ran this long, reset failure count


def _write_status(connector_dir: Path, restarts: int, consecutive_failures: int,
                   backoff: float, gave_up: bool, last_exit_code: int | None) -> None:
    """Write supervisor status to .status.json atomically (write-tmp + replace)."""
    status = {
        "restarts": restarts,
        "consecutive_failures": consecutive_failures,
        "backoff": backoff,
        "gave_up": gave_up,
        "last_exit_code": last_exit_code,
        "timestamp": time.time(),
    }
    status_file = connector_dir / ".status.json"
    tmp_file = status_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(status))
    tmp_file.replace(status_file)


def _supervisor_main(connector_name: str) -> None:
    """Supervisor loop: run connector, restart with backoff on crash.

    This function is invoked as a daemon process by ``_connector_run``.
    It manages the connector child process lifecycle:

    - Start the connector (``run.py``) as a child process
    - On clean exit (code 0): exit the supervisor
    - On crash: restart with exponential backoff
    - If the child ran for >= STABLE_THRESHOLD before crashing, reset the
      consecutive failure counter (it was a real run, not an immediate crash)
    - After SUPERVISOR_MAX_FAILURES consecutive quick failures, give up
    - Forward SIGTERM to child, then exit cleanly
    """
    connector_dir = CONNECTORS_DIR / connector_name
    pid_file = connector_dir / ".pid"
    log_file = connector_dir / "connector.log"

    stop_requested = False
    child_proc: subprocess.Popen | None = None

    def _sigterm_handler(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        if child_proc is not None and child_proc.poll() is None:
            child_proc.terminate()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    backoff = SUPERVISOR_INITIAL_BACKOFF
    consecutive_failures = 0
    total_restarts = 0

    def _log(msg: str) -> None:
        with open(log_file, "a") as f:
            f.write(f"[supervisor] {msg}\n")

    try:
        while not stop_requested:
            log_handle = open(log_file, "a")
            child_proc = subprocess.Popen(
                [".venv/bin/python", "run.py"],
                cwd=str(connector_dir),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            start_time = time.monotonic()
            log_handle.close()

            child_proc.wait()
            exit_code = child_proc.returncode
            elapsed = time.monotonic() - start_time
            child_proc = None

            if stop_requested:
                _log(f"stopped by SIGTERM (child exit code {exit_code})")
                break

            if exit_code == 0:
                _log("child exited cleanly (code 0)")
                break

            # Child crashed
            total_restarts += 1

            if elapsed >= SUPERVISOR_STABLE_THRESHOLD:
                consecutive_failures = 1
                backoff = SUPERVISOR_INITIAL_BACKOFF
                _log(f"child crashed after {elapsed:.0f}s (code {exit_code}), "
                     f"was stable — resetting backoff, restarting")
            else:
                consecutive_failures += 1
                _log(f"child crashed after {elapsed:.1f}s (code {exit_code}), "
                     f"failure {consecutive_failures}/{SUPERVISOR_MAX_FAILURES}")

            _write_status(connector_dir, total_restarts, consecutive_failures,
                          backoff, False, exit_code)

            if consecutive_failures >= SUPERVISOR_MAX_FAILURES:
                _log(f"giving up after {SUPERVISOR_MAX_FAILURES} consecutive failures")
                _write_status(connector_dir, total_restarts, consecutive_failures,
                              backoff, True, exit_code)
                break

            _log(f"waiting {backoff:.1f}s before restart")
            # Interruptible sleep (check stop_requested every 0.1s)
            deadline = time.monotonic() + backoff
            while time.monotonic() < deadline and not stop_requested:
                time.sleep(min(0.1, deadline - time.monotonic()))

            backoff = min(backoff * SUPERVISOR_BACKOFF_MULTIPLIER, SUPERVISOR_MAX_BACKOFF)

    finally:
        pid_file.unlink(missing_ok=True)


def run_connector_command(args) -> None:
    """Dispatch to the appropriate connector subcommand."""
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "connector_command", {
        "list": _connector_list, "search": _connector_search,
        "install": _connector_install, "update": _connector_update,
        "remove": _connector_remove, "run": _connector_run,
        "stop": _connector_stop, "status": _connector_status,
        "test": _connector_test,
    }, "usage: kiso connector {list,search,install,update,remove,run,stop,status,test}")


def _connector_list(args) -> None:
    """List installed connectors."""
    _list_plugins(discover_connectors, "connectors")


def _connector_search(args) -> None:
    """Search official connectors from the registry."""
    registry = fetch_registry()
    results = search_entries(registry.get("connectors", []), args.query)
    _render_search_results(results, args.query, "connector", registry)


def _connector_install(args) -> None:
    """Install a connector from official repo or git URL."""
    require_admin()
    _plugin_install(
        plugin_type="connector",
        official_prefix=OFFICIAL_PREFIX,
        parent_dir=CONNECTORS_DIR,
        validate_fn=_validate_connector_manifest,
        check_deps_fn=check_deps,
        args=args,
        post_install=_connector_post_install,
    )
    invalidate_connectors_cache()


def _connector_update(args) -> None:
    """Update an installed connector or all connectors."""
    require_admin()
    from kiso.sysenv import invalidate_cache
    _update_plugin(
        args.target, CONNECTORS_DIR, "connector", check_deps,
        [invalidate_cache], uv_before_deps=False,
    )


def _connector_remove(args) -> None:
    """Remove an installed connector."""
    require_admin()
    name = args.name
    connector_dir = CONNECTORS_DIR / name
    # Stop the connector if running (before _remove_plugin deletes the dir)
    pid_file = connector_dir / ".pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, OSError):
            pass

    from kiso.sysenv import invalidate_cache
    _remove_plugin(name, connector_dir, "connector", [invalidate_connectors_cache, invalidate_cache])


def _connector_run(args) -> None:
    """Start a connector as a daemon."""
    require_admin()

    name = args.name
    connector_dir = CONNECTORS_DIR / name
    _check_plugin_installed(connector_dir, "connector", name)

    pid_file = connector_dir / ".pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            print(f"error: connector '{name}' is already running (PID {pid})")
            sys.exit(1)
        except (ProcessLookupError, OSError):
            # Stale PID file
            pid_file.unlink()

    # Clear any previous status
    status_file = connector_dir / ".status.json"
    status_file.unlink(missing_ok=True)

    # Spawn supervisor as daemon — it manages the connector child process
    log_file = connector_dir / "connector.log"
    log_handle = open(log_file, "a")

    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"from cli.connector import _supervisor_main; _supervisor_main({name!r})"],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    pid_file.write_text(str(proc.pid))
    log_handle.close()
    print(f"Connector '{name}' started (PID {proc.pid}).")
    from kiso.sysenv import invalidate_cache
    invalidate_cache()


def _connector_stop(args) -> None:
    """Stop a connector daemon."""
    require_admin()

    name = args.name
    connector_dir = CONNECTORS_DIR / name
    _check_plugin_installed(connector_dir, "connector", name)

    pid_file = connector_dir / ".pid"
    if not pid_file.exists():
        print(f"error: connector '{name}' is not running")
        sys.exit(1)

    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        pid_file.unlink()
        print(f"error: connector '{name}' is not running (corrupt PID file)")
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink()
        print(f"error: connector '{name}' is not running (stale PID file)")
        sys.exit(1)

    # Wait up to 5 seconds for process to exit
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        # Still alive after 5s, send SIGKILL
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    pid_file.unlink(missing_ok=True)
    print(f"Connector '{name}' stopped.")
    from kiso.sysenv import invalidate_cache
    invalidate_cache()


def _connector_status(args) -> None:
    """Check connector daemon status."""
    name = args.name
    connector_dir = CONNECTORS_DIR / name
    _check_plugin_installed(connector_dir, "connector", name)

    pid_file = connector_dir / ".pid"
    if not pid_file.exists():
        print(f"Connector '{name}' is not running.")
        return

    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        pid_file.unlink()
        print(f"Connector '{name}' is not running.")
        return

    try:
        os.kill(pid, 0)
        msg = f"Connector '{name}' is running (PID {pid})."
        # Show restart info if available
        status_file = connector_dir / ".status.json"
        if status_file.exists():
            try:
                status = json.loads(status_file.read_text())
                restarts = status.get("restarts", 0)
                if restarts > 0:
                    msg += f" Restarts: {restarts}."
            except (json.JSONDecodeError, OSError):
                pass
        print(msg)
    except ProcessLookupError:
        pid_file.unlink()
        # Check if supervisor gave up
        status_file = connector_dir / ".status.json"
        gave_up_msg = ""
        if status_file.exists():
            try:
                status = json.loads(status_file.read_text())
                if status.get("gave_up"):
                    restarts = status.get("restarts", 0)
                    exit_code = status.get("last_exit_code")
                    gave_up_msg = (
                        f" Supervisor gave up after {restarts} restarts "
                        f"(last exit code: {exit_code})."
                    )
            except (json.JSONDecodeError, OSError):
                pass
        print(f"Connector '{name}' is not running (stale PID file removed).{gave_up_msg}")


def _connector_test(args) -> None:
    """Run a connector's test suite."""
    name = args.name
    connector_dir = CONNECTORS_DIR / name
    _check_plugin_installed(connector_dir, "connector", name)
    test_dir = connector_dir / "tests"
    if not test_dir.exists():
        die(f"connector '{name}' has no tests/ directory")
    venv_python = connector_dir / ".venv" / "bin" / "python"
    cmd = [str(venv_python), "-m", "pytest", "tests/", "-v"]
    result = subprocess.run(cmd, cwd=str(connector_dir), check=False)
    sys.exit(result.returncode)
