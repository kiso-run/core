"""Deploy secret management CLI commands."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from cli.plugin_ops import require_admin
from kiso.config import KISO_DIR

ENV_FILE = KISO_DIR / ".env"


def run_env_command(args) -> None:
    """Dispatch to the appropriate env subcommand."""
    cmd = getattr(args, "env_command", None)
    if cmd is None:
        print("usage: kiso env {set,get,list,delete,reload}")
        sys.exit(1)
    elif cmd == "set":
        _env_set(args)
    elif cmd == "get":
        _env_get(args)
    elif cmd == "list":
        _env_list(args)
    elif cmd == "delete":
        _env_delete(args)
    elif cmd == "reload":
        _env_reload(args)


def _read_lines(path: Path | None = None) -> list[str]:
    """Read all lines from the .env file. Returns empty list if missing."""
    p = path or ENV_FILE
    if not p.is_file():
        return []
    return p.read_text().splitlines()


def _parse_key(line: str) -> str | None:
    """Extract key from a .env line. Returns None for comments/blanks."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        return None
    key, _, _ = stripped.partition("=")
    return key.strip()


def _parse_value(line: str) -> str:
    """Extract value from a .env line."""
    _, _, value = line.strip().partition("=")
    value = value.strip()
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
    ):
        value = value[1:-1]
    return value


def _write_lines(lines: list[str], path: Path | None = None) -> None:
    """Write lines back to the .env file."""
    p = path or ENV_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n" if lines else "")


def _env_set(args) -> None:
    """Set a deploy secret."""
    require_admin()

    key = args.key
    value = args.value

    lines = _read_lines()
    found = False
    for i, line in enumerate(lines):
        if _parse_key(line) == key:
            lines[i] = f"{key}={value}"
            found = True
            break

    if not found:
        lines.append(f"{key}={value}")

    _write_lines(lines)
    print(f"{key} set.")


def _env_get(args) -> None:
    """Get a deploy secret."""
    require_admin()

    key = args.key
    lines = _read_lines()
    for line in lines:
        if _parse_key(line) == key:
            print(_parse_value(line))
            return

    print(f"error: '{key}' not found", file=sys.stderr)
    sys.exit(1)


def _env_list(args) -> None:
    """List all deploy secret names."""
    require_admin()

    lines = _read_lines()
    keys = [k for line in lines if (k := _parse_key(line)) is not None]

    if not keys:
        print("No deploy secrets set.")
        return

    for key in keys:
        print(f"  {key}")


def _env_delete(args) -> None:
    """Delete a deploy secret."""
    require_admin()

    key = args.key
    lines = _read_lines()
    new_lines = [line for line in lines if _parse_key(line) != key]

    if len(new_lines) == len(lines):
        print(f"error: '{key}' not found", file=sys.stderr)
        sys.exit(1)

    _write_lines(new_lines)
    print(f"{key} deleted.")


def _env_reload(args) -> None:
    """Hot-reload .env into the running server."""
    require_admin()

    from cli._http import cli_post

    user = getattr(args, "user", None) or getpass.getuser()
    resp = cli_post(args, "/admin/reload-env", params={"user": user})
    data = resp.json()
    print(f"Reloaded. {data.get('keys_applied', 0)} keys applied, {data.get('keys_skipped', 0)} skipped.")
