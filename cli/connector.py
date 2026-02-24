"""Connector management CLI commands."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from kiso.connectors import (
    CONNECTORS_DIR,
    _connector_env_var_name,
    _validate_connector_manifest,
    discover_connectors,
)
from cli.plugin_ops import (
    OFFICIAL_ORG,
    _GIT_ENV,
    fetch_registry,
    is_repo_not_found,
    is_url,
    require_admin,
    search_entries,
    url_to_name,
)

log = logging.getLogger(__name__)

OFFICIAL_PREFIX = "connector-"

# Supervisor restart settings
SUPERVISOR_MAX_FAILURES = 5
SUPERVISOR_INITIAL_BACKOFF = 1.0  # seconds
SUPERVISOR_MAX_BACKOFF = 60.0  # seconds
SUPERVISOR_BACKOFF_MULTIPLIER = 2.0
SUPERVISOR_STABLE_THRESHOLD = 60.0  # seconds — if child ran this long, reset failure count


def _write_status(connector_dir: Path, restarts: int, consecutive_failures: int,
                   backoff: float, gave_up: bool, last_exit_code: int | None) -> None:
    """Write supervisor status to .status.json."""
    status = {
        "restarts": restarts,
        "consecutive_failures": consecutive_failures,
        "backoff": backoff,
        "gave_up": gave_up,
        "last_exit_code": last_exit_code,
        "timestamp": time.time(),
    }
    status_file = connector_dir / ".status.json"
    status_file.write_text(json.dumps(status))


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
    cmd = getattr(args, "connector_command", None)
    if cmd is None:
        print("usage: kiso connector {list,search,install,update,remove,run,stop,status}")
        sys.exit(1)
    elif cmd == "list":
        _connector_list(args)
    elif cmd == "search":
        _connector_search(args)
    elif cmd == "install":
        _connector_install(args)
    elif cmd == "update":
        _connector_update(args)
    elif cmd == "remove":
        _connector_remove(args)
    elif cmd == "run":
        _connector_run(args)
    elif cmd == "stop":
        _connector_stop(args)
    elif cmd == "status":
        _connector_status(args)


def _connector_list(args) -> None:
    """List installed connectors."""
    connectors = discover_connectors()
    if not connectors:
        print("No connectors installed.")
        return

    max_name = max(len(c["name"]) for c in connectors)
    max_ver = max(len(c["version"]) for c in connectors)
    for c in connectors:
        name = c["name"].ljust(max_name)
        ver = c["version"].ljust(max_ver)
        desc = c.get("description", "")
        print(f"  {name}  {ver}  — {desc}")


def _connector_search(args) -> None:
    """Search official connectors from the registry."""
    registry = fetch_registry()
    results = search_entries(registry.get("connectors", []), args.query)

    if not results:
        print("No connectors found.")
        return

    max_name = max(len(r["name"]) for r in results)
    for r in results:
        print(f"  {r['name'].ljust(max_name)}  — {r.get('description', '')}")


def _connector_install(args) -> None:
    """Install a connector from official repo or git URL."""
    require_admin()

    target = args.target
    if is_url(target):
        git_url = target
        name = args.name or url_to_name(target)
        is_official = False
    else:
        git_url = f"https://github.com/{OFFICIAL_ORG}/{OFFICIAL_PREFIX}{target}.git"
        name = target
        is_official = True

    # --show-deps: clone to temp, show deps.sh, cleanup
    if args.show_deps:
        tmpdir = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                ["git", "clone", git_url, tmpdir],
                capture_output=True, text=True, env=_GIT_ENV,
            )
            if result.returncode != 0:
                if is_official and is_repo_not_found(result.stderr):
                    print(f"error: connector '{name}' not found in {OFFICIAL_ORG} org")
                else:
                    print(f"error: git clone failed: {result.stderr.strip()}")
                sys.exit(1)
            deps_path = Path(tmpdir) / "deps.sh"
            if deps_path.exists():
                print(deps_path.read_text())
            else:
                print("No deps.sh in this connector.")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return

    connector_dir = CONNECTORS_DIR / name

    if connector_dir.exists():
        print(f"error: connector '{name}' is already installed at {connector_dir}")
        sys.exit(1)

    try:
        # Ensure parent dir exists, then clone (creates connector_dir)
        CONNECTORS_DIR.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["git", "clone", git_url, str(connector_dir)],
            capture_output=True, text=True, env=_GIT_ENV,
        )
        if result.returncode != 0:
            if is_official and is_repo_not_found(result.stderr):
                print(f"error: connector '{name}' not found in {OFFICIAL_ORG} org")
            else:
                print(f"error: git clone failed: {result.stderr.strip()}")
            raise RuntimeError("git clone failed")

        # Mark as installing (after clone succeeds)
        (connector_dir / ".installing").touch()

        # Validate manifest
        toml_path = connector_dir / "kiso.toml"
        if not toml_path.exists():
            print("error: kiso.toml not found in cloned repo")
            raise RuntimeError("missing kiso.toml")

        import tomllib

        with open(toml_path, "rb") as f:
            manifest = tomllib.load(f)

        errors = _validate_connector_manifest(manifest, connector_dir)
        if errors:
            for e in errors:
                print(f"error: {e}")
            raise RuntimeError("manifest validation failed")

        # Unofficial repo warning
        if not is_official:
            print("WARNING: This is an unofficial connector repo.")
            deps_path = connector_dir / "deps.sh"
            if deps_path.exists():
                print("\ndeps.sh contents:")
                print(deps_path.read_text())
            answer = input("Continue? [y/N] ").strip().lower()
            if answer != "y":
                print("Installation cancelled.")
                raise RuntimeError("cancelled")

        # Run deps.sh if present and not --no-deps
        deps_path = connector_dir / "deps.sh"
        if deps_path.exists() and not args.no_deps:
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: deps.sh failed: {result.stderr.strip()}")

        # uv sync
        subprocess.run(
            ["uv", "sync"],
            cwd=str(connector_dir),
            capture_output=True, text=True,
        )

        # Check binary deps (parity with skill install)
        from kiso.skills import check_deps
        connector_info = {"path": str(connector_dir)}
        missing = check_deps(connector_info)
        if missing:
            print(f"warning: missing binaries: {', '.join(missing)}")

        # Check env vars
        kiso_section = manifest.get("kiso", {})
        connector_section = kiso_section.get("connector", {})
        env_decl = connector_section.get("env", {})
        connector_name = kiso_section.get("name", name)
        for key in env_decl:
            var_name = _connector_env_var_name(connector_name, key)
            if not os.environ.get(var_name):
                print(f"warning: {var_name} not set")

        # Copy config.example.toml if config.toml doesn't exist
        example_config = connector_dir / "config.example.toml"
        actual_config = connector_dir / "config.toml"
        if example_config.exists() and not actual_config.exists():
            shutil.copy2(example_config, actual_config)
            print(f"note: copied config.example.toml → config.toml (edit before running)")

        # Remove installing marker
        installing = connector_dir / ".installing"
        if installing.exists():
            installing.unlink()

        print(f"Connector '{name}' installed successfully.")
        from kiso.sysenv import invalidate_cache
        invalidate_cache()

    except Exception:
        if connector_dir.exists():
            shutil.rmtree(connector_dir, ignore_errors=True)
        sys.exit(1)


def _connector_update(args) -> None:
    """Update an installed connector or all connectors."""
    require_admin()

    target = args.target
    if target == "all":
        if not CONNECTORS_DIR.is_dir():
            print("No connectors installed.")
            return
        names = [d.name for d in sorted(CONNECTORS_DIR.iterdir()) if d.is_dir()]
        if not names:
            print("No connectors installed.")
            return
    else:
        names = [target]

    for name in names:
        connector_dir = CONNECTORS_DIR / name
        if not connector_dir.exists():
            print(f"error: connector '{name}' is not installed")
            sys.exit(1)

        result = subprocess.run(
            ["git", "pull"],
            cwd=str(connector_dir),
            capture_output=True, text=True, env=_GIT_ENV,
        )
        if result.returncode != 0:
            print(f"error: git pull failed for '{name}': {result.stderr.strip()}")
            sys.exit(1)

        # deps.sh
        deps_path = connector_dir / "deps.sh"
        if deps_path.exists():
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: deps.sh failed for '{name}': {result.stderr.strip()}")

        # uv sync
        subprocess.run(
            ["uv", "sync"],
            cwd=str(connector_dir),
            capture_output=True, text=True,
        )

        # check deps
        from kiso.skills import check_deps
        connector_info = {"path": str(connector_dir)}
        missing = check_deps(connector_info)
        if missing:
            print(f"warning: '{name}' missing binaries: {', '.join(missing)}")

        print(f"Connector '{name}' updated.")
        from kiso.sysenv import invalidate_cache
        invalidate_cache()


def _connector_remove(args) -> None:
    """Remove an installed connector."""
    require_admin()

    name = args.name
    connector_dir = CONNECTORS_DIR / name
    if not connector_dir.exists():
        print(f"error: connector '{name}' is not installed")
        sys.exit(1)

    # Stop the connector if running
    pid_file = connector_dir / ".pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, OSError):
            pass

    shutil.rmtree(connector_dir)
    print(f"Connector '{name}' removed.")
    from kiso.sysenv import invalidate_cache
    invalidate_cache()


def _connector_run(args) -> None:
    """Start a connector as a daemon."""
    require_admin()

    name = args.name
    connector_dir = CONNECTORS_DIR / name
    if not connector_dir.exists():
        print(f"error: connector '{name}' is not installed")
        sys.exit(1)

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
    if not connector_dir.exists():
        print(f"error: connector '{name}' is not installed")
        sys.exit(1)

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
    if not connector_dir.exists():
        print(f"error: connector '{name}' is not installed")
        sys.exit(1)

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
