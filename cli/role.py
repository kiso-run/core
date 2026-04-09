"""`kiso role` CLI subcommand — manage role files in ``~/.kiso/roles/``.

Roles are owned by the user dir at runtime (``KISO_DIR / "roles"``).
The bundled package roles are copied to the user dir at init by
``kiso.main._init_kiso_dirs()``. This CLI provides the recovery and
discovery hatches:

- ``kiso role list``: show user vs package role files.
- ``kiso role reset NAME``: overwrite a single user role file with
  the package version.
- ``kiso role reset --all``: overwrite every user role file that
  has a package counterpart. Custom-only user files (no package
  counterpart) are left alone.
"""

from __future__ import annotations

import argparse
import importlib.resources
import sys
from pathlib import Path

from kiso.config import KISO_DIR


def _package_roles() -> dict[str, str]:
    """Return {role_name: content} for every bundled .md role."""
    out: dict[str, str] = {}
    try:
        roles_pkg = importlib.resources.files("kiso") / "roles"
        for src in roles_pkg.iterdir():
            if src.name.endswith(".md"):
                out[src.name[:-3]] = src.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, TypeError):
        pass
    return out


def role_reset(args: argparse.Namespace) -> None:
    """Overwrite one or all user role files from the package.

    Required args:
    - ``args.name`` (str | None): role name to reset
    - ``args.all`` (bool): if True, reset every package role
    - ``args.yes`` (bool): skip confirmation prompt for non-empty
      existing files
    """
    pkg_roles = _package_roles()
    if not pkg_roles:
        print("error: no bundled roles found in package", file=sys.stderr)
        sys.exit(1)

    roles_dir = KISO_DIR / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)

    if not args.all and not args.name:
        print("error: provide a role NAME or use --all", file=sys.stderr)
        sys.exit(2)

    targets: list[str]
    if args.all:
        targets = sorted(pkg_roles.keys())
    else:
        if args.name not in pkg_roles:
            available = ", ".join(sorted(pkg_roles.keys()))
            print(
                f"error: unknown role '{args.name}'. "
                f"Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)
        targets = [args.name]

    for role in targets:
        target_path = roles_dir / f"{role}.md"
        if target_path.exists() and target_path.stat().st_size > 0 and not args.yes:
            answer = input(
                f"Overwrite existing {target_path}? [y/N] "
            ).strip().lower()
            if answer not in ("y", "yes"):
                print(f"Skipped {role}.md")
                continue
        target_path.write_text(pkg_roles[role], encoding="utf-8")
        print(f"Reset {role}.md")

    # Invalidate the prompt cache so subsequent loads pick up the new content
    try:
        from kiso.brain import invalidate_prompt_cache
        invalidate_prompt_cache()
    except ImportError:
        pass


def role_list(args: argparse.Namespace) -> None:
    """Print user roles vs package roles."""
    pkg_roles = _package_roles()
    roles_dir = KISO_DIR / "roles"

    user_files: dict[str, Path] = {}
    if roles_dir.is_dir():
        for f in roles_dir.glob("*.md"):
            user_files[f.stem] = f

    all_names = sorted(set(pkg_roles.keys()) | set(user_files.keys()))
    if not all_names:
        print("No roles found.")
        return

    print("Roles:")
    for name in all_names:
        in_user = name in user_files
        in_pkg = name in pkg_roles
        if in_user and in_pkg:
            user_size = user_files[name].stat().st_size
            pkg_size = len(pkg_roles[name].encode("utf-8"))
            tag = "custom" if user_size != pkg_size else "default"
            print(f"  {name:<22} [{tag}] (user: {user_size}b, pkg: {pkg_size}b)")
        elif in_user:
            print(f"  {name:<22} [custom-only] (user: {user_files[name].stat().st_size}b)")
        elif in_pkg:
            print(f"  {name:<22} [pkg-only, run `kiso role reset {name}`]")


def run_role_command(args: argparse.Namespace) -> None:
    """Dispatch `kiso role <subcommand>` to the right handler."""
    sub = getattr(args, "role_command", None)
    if sub == "reset":
        role_reset(args)
    elif sub == "list":
        role_list(args)
    else:
        print("error: missing role subcommand (reset | list)", file=sys.stderr)
        sys.exit(2)
