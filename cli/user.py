"""User management CLI commands."""

from __future__ import annotations

import getpass
import json
import sys
import tomllib
from pathlib import Path

import tomli_w

from cli.plugin_ops import require_admin
from kiso.config import CONFIG_PATH, NAME_RE

# Module-level path used by command functions; can be patched in tests.
CONFIG_PATH_DEFAULT: Path = CONFIG_PATH


def run_user_command(args) -> None:
    """Dispatch to the appropriate user subcommand."""
    cmd = getattr(args, "user_command", None)
    if cmd is None:
        print("usage: kiso user {list,add,edit,remove,alias}")
        sys.exit(1)
    elif cmd == "list":
        _user_list(args)
    elif cmd == "add":
        _user_add(args)
    elif cmd == "edit":
        _user_edit(args)
    elif cmd == "remove":
        _user_remove(args)
    elif cmd == "alias":
        _user_alias(args)


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


def _parse_skills(skills_arg: str) -> list[str]:
    """Parse a comma-separated skills string; exits on empty result."""
    skills_list = [s.strip() for s in skills_arg.split(",") if s.strip()]
    if not skills_list:
        print("error: --skills contains no valid skill names", file=sys.stderr)
        sys.exit(1)
    return skills_list


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


def _user_list(args) -> None:
    """List all users with their role, skills, and aliases."""
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
        skills = udata.get("skills", None)
        aliases = udata.get("aliases", {})

        if skills == "*":
            skills_str = "*"
        elif isinstance(skills, list):
            skills_str = ", ".join(skills)
        else:
            skills_str = "-"

        aliases_str = (
            ", ".join(f"{k}:{v}" for k, v in aliases.items()) if aliases else "-"
        )

        print(f"  {name}")
        print(f"    role:    {role}")
        print(f"    skills:  {skills_str}")
        print(f"    aliases: {aliases_str}")


def _user_add(args) -> None:
    """Add a new user to config.toml and reload the server."""
    require_admin()

    username = args.username
    role = args.role
    skills_arg = args.skills
    alias_pairs = args.alias or []

    if not NAME_RE.match(username):
        print(f"error: invalid username '{username}' (must match {NAME_RE.pattern})", file=sys.stderr)
        sys.exit(1)

    if role not in ("admin", "user"):
        print("error: --role must be 'admin' or 'user'", file=sys.stderr)
        sys.exit(1)

    if role == "user" and not skills_arg:
        print(
            "error: --skills required for role=user "
            "(use '*' or a comma-separated list of skill names)",
            file=sys.stderr,
        )
        sys.exit(1)

    aliases: dict[str, str] = {}
    for pair in alias_pairs:
        if ":" not in pair:
            print(f"error: alias '{pair}' must be in 'connector:platform_id' format", file=sys.stderr)
            sys.exit(1)
        connector, platform_id = pair.split(":", 1)
        aliases[connector] = platform_id

    raw = _read_raw()
    if "users" not in raw:
        raw["users"] = {}

    if username in raw["users"]:
        print(f"error: user '{username}' already exists", file=sys.stderr)
        sys.exit(1)

    user_entry: dict = {"role": role}
    if role == "user":
        if skills_arg == "*":
            user_entry["skills"] = "*"
        else:
            user_entry["skills"] = _parse_skills(skills_arg)
    if aliases:
        user_entry["aliases"] = aliases

    raw["users"][username] = user_entry
    _write_raw(raw)
    _maybe_reload(args)
    print(f"User '{username}' added.")


def _user_edit(args) -> None:
    """Edit role and/or skills of an existing user."""
    require_admin()

    username = args.username
    new_role = args.role
    skills_arg = args.skills

    if new_role is None and skills_arg is None:
        print("error: at least one of --role or --skills must be provided", file=sys.stderr)
        sys.exit(1)

    raw = _read_raw()
    users = raw.get("users", {})

    if username not in users:
        print(f"error: user '{username}' does not exist", file=sys.stderr)
        sys.exit(1)

    entry = users[username]
    current_role = entry.get("role")
    final_role = new_role if new_role is not None else current_role

    # Guard: demoting the only admin
    if current_role == "admin" and final_role == "user":
        if not _other_admins(users, username):
            print("error: cannot demote the last admin", file=sys.stderr)
            sys.exit(1)

    # Skills handling
    if skills_arg is not None:
        new_skills = "*" if skills_arg == "*" else _parse_skills(skills_arg)
    else:
        new_skills = entry.get("skills")

    if final_role == "user" and not new_skills:
        print("error: --skills required when role is 'user' and no existing skills are set", file=sys.stderr)
        sys.exit(1)

    entry["role"] = final_role
    if final_role == "user":
        entry["skills"] = new_skills

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
        print(f"error: user '{username}' does not exist", file=sys.stderr)
        sys.exit(1)

    if users[username].get("role") == "admin" and not _other_admins(users, username):
        print("error: cannot remove the last admin", file=sys.stderr)
        sys.exit(1)

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
        print(f"error: invalid connector name '{connector}' (must match {NAME_RE.pattern})", file=sys.stderr)
        sys.exit(1)

    raw = _read_raw()
    users = raw.get("users", {})

    if username not in users:
        print(f"error: user '{username}' does not exist", file=sys.stderr)
        sys.exit(1)

    if remove:
        aliases = users[username].get("aliases", {})
        if connector not in aliases:
            print(
                f"error: user '{username}' has no alias for connector '{connector}'",
                file=sys.stderr,
            )
            sys.exit(1)
        del aliases[connector]
        if aliases:
            raw["users"][username]["aliases"] = aliases
        elif "aliases" in raw["users"][username]:
            del raw["users"][username]["aliases"]
        action = "removed"
    else:
        if not platform_id:
            print("error: --id required when not using --remove", file=sys.stderr)
            sys.exit(1)
        if "aliases" not in raw["users"][username]:
            raw["users"][username]["aliases"] = {}
        raw["users"][username]["aliases"][connector] = platform_id
        action = "set"

    _write_raw(raw)
    _maybe_reload(args)
    print(f"Alias '{connector}' {action} for user '{username}'.")
