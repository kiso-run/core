"""Connector management CLI commands.

Kiso supervises external connector processes declared in
``config.toml`` under ``[connectors.<name>]`` sections. Kiso does not
install connector binaries; users bring their own (via ``uvx``, ``pip``,
``docker``, etc.) and declare a ``command``/``args``/``env``/``cwd``
for kiso to spawn under supervision.

Subcommands:

- ``kiso connector list`` — show configured connectors and their
  current supervisor state.
- ``kiso connector start <name>`` — spawn the command as a daemon under
  the supervisor restart loop.
- ``kiso connector stop <name>`` — send SIGTERM, wait, SIGKILL fallback.
- ``kiso connector status <name>`` — running / stopped / gave up.
- ``kiso connector logs <name>`` — tail of ``connector.log``.
- ``kiso connector add <name>`` — write a ``[connectors.<name>]``
  section to ``config.toml`` (parity with ``kiso mcp add``).
- ``kiso connector migrate`` — scan legacy ``~/.kiso/connectors/<name>/``
  plugin installs and print suggested ``[connectors.<name>]`` blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import tomli_w

from cli.render import die
from kiso.config import CONFIG_PATH
from kiso.connector_config import NAME_RE, ConnectorConfig, ConnectorConfigError, parse_connectors_section
from kiso.connectors import CONNECTORS_DIR, discover_connectors


def _state_dir(name: str) -> Path:
    """Return (and lazily create) the supervisor state dir for this connector.

    Local indirection lets ``monkeypatch.setattr(cli.connector.CONNECTORS_DIR)``
    redirect the path in tests without having to patch the same constant in
    ``kiso.connectors``.
    """
    p = CONNECTORS_DIR / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Supervisor restart settings (unchanged from v0.9 — moved, not rewritten)
# ---------------------------------------------------------------------------

SUPERVISOR_MAX_FAILURES = 5
SUPERVISOR_INITIAL_BACKOFF = 1.0
SUPERVISOR_MAX_BACKOFF = 60.0
SUPERVISOR_BACKOFF_MULTIPLIER = 2.0
SUPERVISOR_STABLE_THRESHOLD = 60.0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def run_connector_command(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate connector subcommand."""
    cmd = getattr(args, "connector_command", None)
    handlers = {
        "list": _connector_list,
        "start": _connector_start,
        "stop": _connector_stop,
        "status": _connector_status,
        "logs": _connector_logs,
        "add": _connector_add,
        "migrate": _connector_migrate,
    }
    handler = handlers.get(cmd)
    if handler is None:
        die("usage: kiso connector {list,start,stop,status,logs,add,migrate}")
    handler(args)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _connector_list(args: argparse.Namespace) -> None:
    connectors = discover_connectors()
    if not connectors:
        print("(no connectors configured)")
        print(f"Config: {CONFIG_PATH}")
        return
    print(f"{'NAME':<20} {'STATUS':<10} {'ENABLED':<8} COMMAND")
    for c in connectors:
        status = _state_of(c["name"])
        enabled = "yes" if c["enabled"] else "no"
        detail = c["command"] + (" " + " ".join(c["args"]) if c["args"] else "")
        print(f"{c['name']:<20} {status:<10} {enabled:<8} {detail}")


def _state_of(name: str) -> str:
    pid_file = CONNECTORS_DIR / name / ".pid"
    if not pid_file.exists():
        return "stopped"
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return "running"
    except (ValueError, ProcessLookupError, OSError):
        return "stopped"


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _connector_start(args: argparse.Namespace) -> None:
    """Spawn the supervisor as a detached daemon."""
    _require_admin()
    name = args.name
    connector = _load_connector(name)
    if not connector.enabled:
        die(f"connector '{name}' is disabled in config.toml")

    state_dir = _state_dir(name)
    pid_file = state_dir / ".pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            print(f"error: connector '{name}' is already running (PID {pid})")
            sys.exit(1)
        except (ProcessLookupError, OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    (state_dir / ".status.json").unlink(missing_ok=True)

    log_file = state_dir / "connector.log"
    log_handle = open(log_file, "a")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from cli.connector import _supervisor_main; _supervisor_main({name!r})",
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))
    log_handle.close()
    print(f"Connector '{name}' started (PID {proc.pid}).")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def _connector_stop(args: argparse.Namespace) -> None:
    _require_admin()
    name = args.name
    _load_connector(name)  # verify declared — supervisor PID file may exist independently
    pid_file = CONNECTORS_DIR / name / ".pid"
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

    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    pid_file.unlink(missing_ok=True)
    print(f"Connector '{name}' stopped.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _connector_status(args: argparse.Namespace) -> None:
    name = args.name
    _load_connector(name)  # verify declared
    pid_file = CONNECTORS_DIR / name / ".pid"
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
    except ProcessLookupError:
        pid_file.unlink()
        gave_up_msg = ""
        status_file = CONNECTORS_DIR / name / ".status.json"
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
        return

    msg = f"Connector '{name}' is running (PID {pid})."
    status_file = CONNECTORS_DIR / name / ".status.json"
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text())
            restarts = status.get("restarts", 0)
            if restarts > 0:
                msg += f" Restarts: {restarts}."
        except (json.JSONDecodeError, OSError):
            pass
    print(msg)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def _connector_logs(args: argparse.Namespace) -> None:
    name = args.name
    _load_connector(name)
    log_file = CONNECTORS_DIR / name / "connector.log"
    if not log_file.exists():
        print(f"(no log yet for connector '{name}')")
        return
    lines = log_file.read_text(errors="replace").splitlines()
    tail_n = int(getattr(args, "n", 50) or 50)
    for line in lines[-tail_n:]:
        print(line)


# ---------------------------------------------------------------------------
# add — direct-form parity with `kiso mcp add`
# ---------------------------------------------------------------------------


def _connector_add(args: argparse.Namespace) -> None:
    _require_admin()
    name = args.name
    if not NAME_RE.match(name):
        die(f"invalid connector name: {name!r} (must match {NAME_RE.pattern})")
    if not args.command:
        die("--command is required")

    entry: dict = {"command": args.command}
    if args.args:
        entry["args"] = list(args.args)
    if args.cwd:
        entry["cwd"] = args.cwd
    if args.token:
        entry["token"] = args.token
    if args.webhook:
        entry["webhook"] = args.webhook
    env = _parse_kv_pairs(args.env, "--env")
    if env:
        entry["env"] = env

    path, raw = _read_config_raw()
    connectors = raw.get("connectors") or {}
    if not isinstance(connectors, dict):
        die("existing [connectors] section is malformed")
    connectors[name] = entry
    raw["connectors"] = connectors

    # Validate via the canonical parser before writing.
    try:
        parse_connectors_section(connectors)
    except ConnectorConfigError as e:
        die(f"rejected connector entry: {e}")

    _write_config_raw(path, raw)
    print(f"wrote [connectors.{name}] to {path}")


def _parse_kv_pairs(raw: list[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw:
        return out
    for item in raw:
        if "=" not in item:
            die(f"{flag} entry must be KEY=VAL, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            die(f"{flag} entry has empty key: {item!r}")
        out[key] = value
    return out


def _read_config_raw(config_path: Path | None = None) -> tuple[Path, dict]:
    import tomllib

    path = config_path or CONFIG_PATH
    if not path.exists():
        die(f"config file not found: {path}")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return path, raw


def _write_config_raw(path: Path, raw: dict) -> None:
    path.write_bytes(tomli_w.dumps(raw).encode("utf-8"))


# ---------------------------------------------------------------------------
# migrate — help users move from legacy plugin-install layout to config
# ---------------------------------------------------------------------------


def _connector_migrate(args: argparse.Namespace) -> None:
    """Scan legacy ``~/.kiso/connectors/<name>/`` dirs and suggest config blocks."""
    if not CONNECTORS_DIR.is_dir():
        print("(no legacy connector directory found)")
        return
    legacy = [
        p for p in CONNECTORS_DIR.iterdir()
        if p.is_dir() and (p / "kiso.toml").exists()
    ]
    if not legacy:
        print("(no legacy connector installs found)")
        return

    print("# Add the following to your config.toml (adjust paths as needed):")
    print()
    for p in sorted(legacy, key=lambda x: x.name):
        name = p.name
        print(f"[connectors.{name}]")
        run_py = p / "run.py"
        if run_py.exists():
            print(f'command = "{p / ".venv" / "bin" / "python"}"')
            print(f'args = ["{run_py}"]')
        else:
            print(f'command = "uvx"')
            print(f'args = ["{name}-connector"]  # adjust')
        print()


# ---------------------------------------------------------------------------
# Supervisor daemon (loaded lazily by `kiso connector start`)
# ---------------------------------------------------------------------------


def _supervisor_main(
    connector_name: str,
    connector: ConnectorConfig | None = None,
) -> None:
    """Supervisor loop: spawn the declared command, restart with backoff on crash.

    Invoked as a daemon by ``_connector_start``. Lifecycle:

    - Load the connector's config (unless passed directly for tests).
    - Spawn ``command + args`` in ``cwd`` with merged ``env``; stream
      stdout/stderr to ``connector.log``.
    - On clean exit (code 0): exit the supervisor.
    - On crash: restart with exponential backoff.
    - Stable-run reset: if the child ran for >= STABLE_THRESHOLD before
      crashing, reset the consecutive-failure counter.
    - After SUPERVISOR_MAX_FAILURES consecutive quick failures, give up.
    - Forward SIGTERM to the child, then exit cleanly.
    """
    if connector is None:
        from kiso.config import load_config

        config = load_config()
        connector = config.connectors.get(connector_name)
        if connector is None:
            raise SystemExit(f"connector '{connector_name}' not declared in config.toml")

    state_dir = _state_dir(connector_name)
    pid_file = state_dir / ".pid"
    log_file = state_dir / "connector.log"

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

    cmd_argv = [connector.command, *connector.args]
    cwd = connector.cwd or None
    env = {**os.environ, **connector.env}

    try:
        while not stop_requested:
            log_handle = open(log_file, "a")
            child_proc = subprocess.Popen(
                cmd_argv,
                cwd=cwd,
                env=env,
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

            total_restarts += 1

            if elapsed >= SUPERVISOR_STABLE_THRESHOLD:
                consecutive_failures = 1
                backoff = SUPERVISOR_INITIAL_BACKOFF
                _log(
                    f"child crashed after {elapsed:.0f}s (code {exit_code}), "
                    f"was stable — resetting backoff, restarting"
                )
            else:
                consecutive_failures += 1
                _log(
                    f"child crashed after {elapsed:.1f}s (code {exit_code}), "
                    f"failure {consecutive_failures}/{SUPERVISOR_MAX_FAILURES}"
                )

            _write_status(
                state_dir, total_restarts, consecutive_failures, backoff, False, exit_code
            )

            if consecutive_failures >= SUPERVISOR_MAX_FAILURES:
                _log(f"giving up after {SUPERVISOR_MAX_FAILURES} consecutive failures")
                _write_status(
                    state_dir, total_restarts, consecutive_failures, backoff, True, exit_code
                )
                break

            _log(f"waiting {backoff:.1f}s before restart")
            deadline = time.monotonic() + backoff
            while time.monotonic() < deadline and not stop_requested:
                time.sleep(min(0.1, deadline - time.monotonic()))

            backoff = min(backoff * SUPERVISOR_BACKOFF_MULTIPLIER, SUPERVISOR_MAX_BACKOFF)

    finally:
        pid_file.unlink(missing_ok=True)


def _write_status(
    state_dir: Path,
    restarts: int,
    consecutive_failures: int,
    backoff: float,
    gave_up: bool,
    last_exit_code: int | None,
) -> None:
    """Write supervisor status to .status.json atomically (write-tmp + replace)."""
    status = {
        "restarts": restarts,
        "consecutive_failures": consecutive_failures,
        "backoff": backoff,
        "gave_up": gave_up,
        "last_exit_code": last_exit_code,
        "timestamp": time.time(),
    }
    status_file = state_dir / ".status.json"
    tmp_file = status_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(status))
    tmp_file.replace(status_file)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_connector(name: str) -> ConnectorConfig:
    """Load the named connector from config.toml or die."""
    from kiso.config import load_config

    config = load_config()
    connector = config.connectors.get(name)
    if connector is None:
        die(f"connector '{name}' is not declared in config.toml")
    return connector


def _require_admin() -> None:
    """Connector lifecycle is admin-only to avoid surprising other users."""
    try:
        from cli.plugin_ops import require_admin
    except ImportError:
        return
    require_admin()
