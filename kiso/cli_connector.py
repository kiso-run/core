"""Connector management CLI commands."""

from __future__ import annotations

import getpass
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from kiso.cli_skill import _is_url, _require_admin, url_to_name
from kiso.config import KISO_DIR

CONNECTORS_DIR = KISO_DIR / "connectors"
OFFICIAL_ORG = "kiso-run"
OFFICIAL_PREFIX = "connector-"
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"


def _validate_connector_manifest(manifest: dict, connector_dir: Path) -> list[str]:
    """Validate a kiso.toml manifest for connectors. Returns list of error strings."""
    errors: list[str] = []

    kiso = manifest.get("kiso")
    if not isinstance(kiso, dict):
        errors.append("missing [kiso] section")
        return errors

    if kiso.get("type") != "connector":
        errors.append(f"kiso.type must be 'connector', got {kiso.get('type')!r}")

    if not kiso.get("name") or not isinstance(kiso.get("name"), str):
        errors.append("kiso.name is required and must be a string")

    connector_section = kiso.get("connector")
    if not isinstance(connector_section, dict):
        errors.append("missing [kiso.connector] section")
        return errors

    if not (connector_dir / "run.py").exists():
        errors.append("run.py is missing")
    if not (connector_dir / "pyproject.toml").exists():
        errors.append("pyproject.toml is missing")

    return errors


def _connector_env_var_name(connector_name: str, key: str) -> str:
    """Build env var name: KISO_CONNECTOR_{NAME}_{KEY}."""
    name_part = connector_name.upper().replace("-", "_")
    key_part = key.upper().replace("-", "_")
    return f"KISO_CONNECTOR_{name_part}_{key_part}"


def discover_connectors(connectors_dir: Path | None = None) -> list[dict]:
    """Scan ~/.kiso/connectors/ and return list of valid connector info dicts.

    Each dict has: name, version, description, platform, path.

    Skips directories with .installing marker.
    """
    connectors_dir = connectors_dir or CONNECTORS_DIR
    if not connectors_dir.is_dir():
        return []

    import tomllib

    connectors: list[dict] = []
    for entry in sorted(connectors_dir.iterdir()):
        if not entry.is_dir():
            continue

        if (entry / ".installing").exists():
            continue

        toml_path = entry / "kiso.toml"
        if not toml_path.exists():
            continue

        try:
            with open(toml_path, "rb") as f:
                manifest = tomllib.load(f)
        except Exception:
            continue

        errors = _validate_connector_manifest(manifest, entry)
        if errors:
            continue

        kiso = manifest["kiso"]
        connector_section = kiso.get("connector", {})

        connectors.append({
            "name": kiso["name"],
            "version": kiso.get("version", "0.0.0"),
            "description": kiso.get("description", ""),
            "platform": connector_section.get("platform", ""),
            "path": str(entry),
        })

    return connectors


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
    """Search official connectors on GitHub."""
    import httpx

    query_parts = ["org:kiso-run", "topic:kiso-connector"]
    if args.query:
        query_parts.append(args.query)
    q = "+".join(query_parts)

    try:
        resp = httpx.get(GITHUB_SEARCH_URL, params={"q": q}, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"error: GitHub search failed: {exc}")
        sys.exit(1)

    data = resp.json()
    items = data.get("items", [])
    if not items:
        print("No connectors found.")
        return

    results = []
    for item in items:
        name = item["name"]
        if name.startswith(OFFICIAL_PREFIX):
            name = name[len(OFFICIAL_PREFIX):]
        desc = item.get("description", "")
        results.append((name, desc))

    max_name = max(len(r[0]) for r in results)
    for name, desc in results:
        print(f"  {name.ljust(max_name)}  — {desc}")


def _connector_install(args) -> None:
    """Install a connector from official repo or git URL."""
    _require_admin()

    target = args.target
    if _is_url(target):
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
                capture_output=True, text=True,
            )
            if result.returncode != 0:
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
        connector_dir.mkdir(parents=True, exist_ok=True)
        (connector_dir / ".installing").touch()

        result = subprocess.run(
            ["git", "clone", git_url, str(connector_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"error: git clone failed: {result.stderr.strip()}")
            raise RuntimeError("git clone failed")

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
    _require_admin()

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
            capture_output=True, text=True,
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

        print(f"Connector '{name}' updated.")
        from kiso.sysenv import invalidate_cache
        invalidate_cache()


def _connector_remove(args) -> None:
    """Remove an installed connector."""
    _require_admin()

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
    _require_admin()

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

    log_file = connector_dir / "connector.log"
    log_handle = open(log_file, "a")

    proc = subprocess.Popen(
        [".venv/bin/python", "run.py"],
        cwd=str(connector_dir),
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
    _require_admin()

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
        print(f"Connector '{name}' is running (PID {pid}).")
    except ProcessLookupError:
        pid_file.unlink()
        print(f"Connector '{name}' is not running (stale PID file removed).")
