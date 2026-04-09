"""`kiso roles` CLI subcommand — discover, inspect, and reset role files.

Roles are owned by the user dir at runtime (``KISO_DIR / "roles"``).
This module provides the user-facing surface backed by the
``kiso.brain.roles_registry`` single source of truth:

- ``kiso roles list``: tabular view of every registered role with
  its default model, description, and user-override status.
- ``kiso roles show NAME``: print the resolved prompt (user override
  if present, bundled default otherwise) preceded by a model header.
- ``kiso roles diff NAME``: unified diff between user override and
  bundled default; prints "no user override" when absent.
- ``kiso roles reset NAME``: delegate to the existing ``cli.role``
  reset path so the singular form keeps working.

The singular ``kiso role`` form is preserved as a deprecated alias
for one cycle to avoid breaking existing scripts.
"""

from __future__ import annotations

import argparse
import difflib
import sys

from kiso.brain.roles_registry import get_role, list_roles
from kiso.config import KISO_DIR


def _bundled_text(role_name: str) -> str | None:
    """Return the bundled default prompt for *role_name*, or None."""
    from cli.role import _package_roles
    pkg = _package_roles()
    meta = get_role(role_name)
    if meta is None:
        return pkg.get(role_name)
    # Registry name == filename stem in the bundle
    stem = meta.prompt_filename[:-3] if meta.prompt_filename.endswith(".md") else meta.prompt_filename
    return pkg.get(stem)


def _user_text(role_name: str) -> str | None:
    """Return the user-override prompt for *role_name*, or None."""
    meta = get_role(role_name)
    filename = meta.prompt_filename if meta else f"{role_name}.md"
    path = KISO_DIR / "roles" / filename
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def roles_list(args: argparse.Namespace) -> None:
    """Print every registered role with model, description, and override flag."""
    rows = []
    name_w = max(len(r.name) for r in list_roles())
    model_w = max(len(r.default_model) for r in list_roles())
    for r in list_roles():
        bundled = _bundled_text(r.name)
        user = _user_text(r.name)
        if user is not None and bundled is not None and user != bundled:
            override = "user override"
        else:
            override = ""
        rows.append((r.name, r.default_model, override, r.description))

    print("Roles:")
    for name, model, override, desc in rows:
        marker = f"[{override}] " if override else ""
        print(f"  {name:<{name_w}}  {model:<{model_w}}  {marker}{desc}")


def roles_show(args: argparse.Namespace) -> None:
    """Print the resolved prompt for one role, with a 2-line header."""
    name = args.name
    meta = get_role(name)
    if meta is None:
        print(f"error: unknown role '{name}'", file=sys.stderr)
        sys.exit(1)
    user = _user_text(name)
    bundled = _bundled_text(name)
    text = user if user is not None else bundled
    if text is None:
        print(
            f"error: no prompt file found for role '{name}' "
            f"(neither user override nor bundled default)",
            file=sys.stderr,
        )
        sys.exit(1)
    source = "user override" if user is not None else "bundled default"
    print(f"# role: {name}  model: {meta.default_model}  source: {source}")
    print(f"# entry: {meta.python_entry}")
    print()
    print(text)


def roles_diff(args: argparse.Namespace) -> None:
    """Print a unified diff of user override vs bundled default."""
    name = args.name
    meta = get_role(name)
    if meta is None:
        print(f"error: unknown role '{name}'", file=sys.stderr)
        sys.exit(1)
    user = _user_text(name)
    bundled = _bundled_text(name)
    if user is None:
        print(f"no user override for '{name}' (matches bundled default).")
        return
    if bundled is None:
        print(
            f"error: no bundled default for role '{name}' to diff against",
            file=sys.stderr,
        )
        sys.exit(1)
    if user == bundled:
        print(f"no user override for '{name}' (matches bundled default).")
        return
    diff = difflib.unified_diff(
        bundled.splitlines(keepends=True),
        user.splitlines(keepends=True),
        fromfile=f"bundled/{meta.prompt_filename}",
        tofile=f"user/{meta.prompt_filename}",
        n=3,
    )
    sys.stdout.writelines(diff)


def roles_reset(args: argparse.Namespace) -> None:
    """Delegate to the existing ``cli.role.role_reset`` path."""
    from cli.role import role_reset
    role_reset(args)


def run_roles_command(args: argparse.Namespace) -> None:
    """Dispatch ``kiso roles <subcommand>`` to the right handler."""
    sub = getattr(args, "roles_command", None)
    if sub == "list":
        roles_list(args)
    elif sub == "show":
        roles_show(args)
    elif sub == "diff":
        roles_diff(args)
    elif sub == "reset":
        roles_reset(args)
    else:
        print(
            "error: missing roles subcommand (list | show | diff | reset)",
            file=sys.stderr,
        )
        sys.exit(2)
