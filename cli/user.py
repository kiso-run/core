"""User management CLI commands."""

from __future__ import annotations

import getpass
import json
import pwd
import sys
import tomllib
from pathlib import Path

import tomli_w

from cli.plugin_ops import require_admin
from cli.render import die
from kiso.config import CONFIG_PATH, NAME_RE

# Module-level path used by command functions; can be patched in tests.
CONFIG_PATH_DEFAULT: Path = CONFIG_PATH


def run_user_command(args) -> None:
    """Dispatch to the appropriate user subcommand."""
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "user_command", {
        "list": _user_list, "add": _user_add, "edit": _user_edit,
        "remove": _user_remove, "alias": _user_alias,
    }, "usage: kiso user {list,add,edit,remove,alias}")


def _read_raw(path: Path | None = None) -> dict:
    """Read and parse config.toml. Returns the raw dict, or {} if the file doesn't exist."""
    p = path or CONFIG_PATH_DEFAULT
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _write_raw(raw: dict, path: Path | None = None) -> None:
    """Write raw dict back to config.toml using tomli_w."""
    p = path or CONFIG_PATH_DEFAULT
    with open(p, "wb") as f:
        tomli_w.dump(raw, f)


def _parse_wrappers(wrappers_arg: str) -> list[str]:
    """Parse a comma-separated wrappers string; exits on empty result."""
    wrappers_list = [s.strip() for s in wrappers_arg.split(",") if s.strip()]
    if not wrappers_list:
        die("--wrappers contains no valid wrapper names")
    return wrappers_list


def _other_admins(users: dict, exclude: str) -> list[str]:
    """Return admin usernames other than *exclude*."""
    return [
        name for name, udata in users.items()
        if udata.get("role") == "admin" and name != exclude
    ]


def _maybe_reload(args) -> None:
    """Call _call_reload unless --no-reload was passed."""
    if not getattr(args, "no_reload", False):
        _call_reload(args)


def _call_reload(args) -> None:
    """Call POST /admin/reload-config to hot-reload the running server."""
    from cli._http import cli_post

    user = getattr(args, "user", None) or getpass.getuser()
    cli_post(args, "/admin/reload-config", params={"user": user})


def _system_user_exists(username: str) -> bool:
    """Check if a Linux system user exists via the passwd database."""
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def _user_list(args) -> None:
    """List all users with their role, wrappers, and aliases."""
    require_admin()

    raw = _read_raw()
    users = raw.get("users", {})

    if getattr(args, "json", False):
        print(json.dumps(users, indent=2))
        return

    if not users:
        print("No users configured.")
        return

    for name, udata in users.items():
        role = udata.get("role", "?")
        wrappers = udata.get("wrappers", None)
        aliases = udata.get("aliases", {})

        if wrappers == "*":
            wrappers_str = "*"
        elif isinstance(wrappers, list):
            wrappers_str = ", ".join(wrappers)
        else:
            wrappers_str = "-"

        aliases_str = (
            ", ".join(f"{k}:{v}" for k, v in aliases.items()) if aliases else "-"
        )

        print(f"  {name}")
        print(f"    role:    {role}")
        print(f"    wrappers:  {wrappers_str}")
        print(f"    aliases: {aliases_str}")


def _user_add(args) -> None:
    """Add a new user to config.toml and reload the server."""
    require_admin()

    username = args.username
    role = args.role
    wrappers_arg = args.wrappers
    alias_pairs = args.alias or []

    if not NAME_RE.match(username):
        die(f"invalid user '{username}' (must match {NAME_RE.pattern})")

    # Check if the Linux system user exists; warn with instructions if not
    if not _system_user_exists(username):
        print(
            f"warning: Linux user '{username}' does not exist on this system.\n"
            f"  Kiso users map to Linux system users. To create one, run on the host:\n"
            f"    sudo useradd -m {username}\n"
            f"  The kiso config entry will be created now, but won't be functional\n"
            f"  until the Linux user exists and the container is restarted.",
            file=sys.stderr,
        )

    if role not in ("admin", "user"):
        die("--role must be 'admin' or 'user'")

    if role == "user" and not wrappers_arg:
        die("--wrappers required for role=user (use '*' or a comma-separated list of wrapper names)")

    aliases: dict[str, str] = {}
    for pair in alias_pairs:
        if ":" not in pair:
            die(f"alias '{pair}' must be in 'connector:platform_id' format")
        connector, platform_id = pair.split(":", 1)
        aliases[connector] = platform_id

    raw = _read_raw()
    if "users" not in raw:
        raw["users"] = {}

    if username in raw["users"]:
        die(f"user '{username}' already exists")

    user_entry: dict = {"role": role}
    if role == "user":
        if wrappers_arg == "*":
            user_entry["wrappers"] = "*"
        else:
            user_entry["wrappers"] = _parse_wrappers(wrappers_arg)
    if aliases:
        user_entry["aliases"] = aliases

    raw["users"][username] = user_entry
    _write_raw(raw)
    _maybe_reload(args)
    print(f"User '{username}' added.")


def _user_edit(args) -> None:
    """Edit role and/or wrappers of an existing user."""
    require_admin()

    username = args.username
    new_role = args.role
    wrappers_arg = args.wrappers

    if new_role is None and wrappers_arg is None:
        die("at least one of --role or --wrappers must be provided")

    raw = _read_raw()
    users = raw.get("users", {})

    if username not in users:
        die(f"user '{username}' does not exist")

    entry = users[username]
    current_role = entry.get("role")
    final_role = new_role if new_role is not None else current_role

    # Guard: demoting the only admin
    if current_role == "admin" and final_role == "user":
        if not _other_admins(users, username):
            die("cannot demote the last admin")

    # Wrappers handling
    if wrappers_arg is not None:
        new_wrappers = "*" if wrappers_arg == "*" else _parse_wrappers(wrappers_arg)
    else:
        new_wrappers = entry.get("wrappers")

    if final_role == "user" and not new_wrappers:
        die("--wrappers required when role is 'user' and no existing wrappers are set")

    entry["role"] = final_role
    if final_role == "user":
        entry["wrappers"] = new_wrappers

    _write_raw(raw)
    _maybe_reload(args)
    print(f"User '{username}' updated.")


def _user_remove(args) -> None:
    """Remove a user from config.toml and reload the server."""
    require_admin()

    username = args.username

    raw = _read_raw()
    users = raw.get("users", {})

    if username not in users:
        die(f"user '{username}' does not exist")

    if users[username].get("role") == "admin" and not _other_admins(users, username):
        die("cannot remove the last admin")

    del raw["users"][username]
    _write_raw(raw)
    _maybe_reload(args)
    print(f"User '{username}' removed.")


def _user_alias(args) -> None:
    """Add, update, or remove a connector alias for an existing user."""
    require_admin()

    username = args.username
    connector = args.connector
    remove = args.remove
    platform_id = getattr(args, "id", None)

    if not NAME_RE.match(connector):
        die(f"invalid connector name '{connector}' (must match {NAME_RE.pattern})")

    raw = _read_raw()
    users = raw.get("users", {})

    if username not in users:
        die(f"user '{username}' does not exist")

    if remove:
        aliases = users[username].get("aliases", {})
        if connector not in aliases:
            die(f"user '{username}' has no alias for connector '{connector}'")
        del aliases[connector]
        if aliases:
            raw["users"][username]["aliases"] = aliases
        elif "aliases" in raw["users"][username]:
            del raw["users"][username]["aliases"]
        action = "removed"
    else:
        if not platform_id:
            die("--id required when not using --remove")
        if "aliases" not in raw["users"][username]:
            raw["users"][username]["aliases"] = {}
        raw["users"][username]["aliases"][connector] = platform_id
        action = "set"

    _write_raw(raw)
    _maybe_reload(args)
    print(f"Alias '{connector}' {action} for user '{username}'.")
