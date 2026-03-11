"""Shared utilities for skill and connector CLI operations."""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from kiso.config import KISO_DIR, load_config

OFFICIAL_ORG = "kiso-run"
REGISTRY_URL = "https://raw.githubusercontent.com/kiso-run/core/main/registry.json"

# Prevent git from opening /dev/tty to prompt for credentials.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def url_to_name(url: str) -> str:
    """Convert a git URL to a plugin install name.

    Algorithm (from docs/skills.md):
    1. Strip .git suffix
    2. Normalize SSH (git@host:ns/repo -> host/ns/repo) and HTTPS (strip scheme)
    3. Lowercase
    4. Replace . with - (domain)
    5. Replace / with _
    """
    name = url
    if name.endswith(".git"):
        name = name[:-4]
    if name.startswith("git@"):
        name = name[4:]
        name = name.replace(":", "/", 1)
    name = re.sub(r"^https?://", "", name)
    name = name.lower()
    name = name.replace(".", "-")
    name = name.replace("/", "_")
    return name


def is_url(target: str) -> bool:
    """Return True if target looks like a git URL."""
    return target.startswith(("git@", "http://", "https://"))


def is_repo_not_found(stderr: str) -> bool:
    """Detect git clone failures caused by a nonexistent repo.

    Git outputs different messages depending on auth setup:
    - "not found" — when the server explicitly returns 404
    - "terminal prompts disabled" — when GIT_TERMINAL_PROMPT=0 and repo
      doesn't exist (GitHub returns a credential challenge for 404s)
    """
    s = stderr.lower()
    return "not found" in s or "terminal prompts disabled" in s


def require_admin() -> None:
    """Check that the current Linux user is an admin in kiso config. Exits 1 if not."""
    username = getpass.getuser()
    if username == "root" and os.getuid() == 0:
        return  # running inside the container as root — skip check
    cfg = load_config()
    user = cfg.users.get(username)
    if user is None:
        print(f"error: unknown user '{username}'")
        sys.exit(1)
    if user.role != "admin":
        print(f"error: user '{username}' is not an admin")
        sys.exit(1)


def fetch_registry() -> dict:
    """Fetch the official registry from GitHub (raw file, no API)."""
    import json

    import httpx

    try:
        resp = httpx.get(REGISTRY_URL, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        return json.loads(resp.text)
    except httpx.HTTPError as exc:
        print(f"error: failed to fetch registry: {exc}")
        sys.exit(1)
    except (json.JSONDecodeError, KeyError):
        print("error: invalid registry format")
        sys.exit(1)


def search_entries(entries: list[dict], query: str | None) -> list[dict]:
    """Filter registry entries: match name first, then description."""
    if not query:
        return entries
    q = query.lower()
    by_name = [e for e in entries if q in e["name"].lower()]
    if by_name:
        return by_name
    return [e for e in entries if q in e.get("description", "").lower()]


def cross_type_hint(registry: dict, current_type: str, query: str) -> str | None:
    """Check the other plugin type for matches and return a hint string.

    When a search in one type (e.g. "connectors") yields no results, this
    function checks the other type ("skills") for matches.  Returns a hint
    string like 'Did you mean `kiso skill search browser`?' or None.
    """
    if current_type == "connectors":
        other_type = "tools"
        other_cmd = "tool"
    else:
        other_type = "connectors"
        other_cmd = "connector"
    other_entries = registry.get(other_type, registry.get("skills", []) if other_type == "tools" else [])
    matches = search_entries(other_entries, query)
    if matches:
        names = ", ".join(m["name"] for m in matches)
        return f"Did you mean `kiso {other_cmd} search {query}`? Found in {other_type}: {names}"
    return None


def _plugin_install(
    plugin_type: str,
    official_prefix: str,
    parent_dir: Path,
    validate_fn,
    check_deps_fn,
    args,
    post_install=None,
) -> None:
    """Shared install logic for skills and connectors.

    Args:
        plugin_type: "skill" or "connector" — used in user-facing messages.
        official_prefix: Git repo name prefix ("skill-" or "connector-").
        parent_dir: Directory where the plugin is installed (SKILLS_DIR/CONNECTORS_DIR).
        validate_fn: callable(manifest, plugin_dir) -> list[str] — manifest validator.
        check_deps_fn: callable(plugin_info) -> list[str] — binary deps checker.
        args: argparse Namespace with .target, .name, .show_deps, .no_deps.
        post_install: optional callable(manifest, plugin_dir, name) for type-specific
            post-install steps (env var warnings, config copy, usage guide, etc.).
    """
    from kiso.sysenv import invalidate_cache

    target = args.target
    if is_url(target):
        git_url = target
        name = args.name or url_to_name(target)
        is_official = False
    else:
        git_url = f"https://github.com/{OFFICIAL_ORG}/{official_prefix}{target}.git"
        name = target
        is_official = True

    # --show-deps: clone to temp, show deps.sh, then cleanup without installing
    if args.show_deps:
        tmpdir = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                ["git", "clone", git_url, tmpdir],
                capture_output=True, text=True, env=_GIT_ENV,
            )
            if result.returncode != 0:
                if is_official and is_repo_not_found(result.stderr):
                    print(f"error: {plugin_type} '{name}' not found in {OFFICIAL_ORG} org")
                else:
                    print(f"error: git clone failed: {result.stderr.strip()}")
                sys.exit(1)
            deps_path = Path(tmpdir) / "deps.sh"
            if deps_path.exists():
                print(deps_path.read_text())
            else:
                print(f"No deps.sh in this {plugin_type}.")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return

    plugin_dir = parent_dir / name

    if plugin_dir.exists():
        print(f"{plugin_type.capitalize()} '{name}' is already installed at {plugin_dir}")
        return  # idempotent — desired state achieved

    try:
        parent_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["git", "clone", git_url, str(plugin_dir)],
            capture_output=True, text=True, env=_GIT_ENV,
        )
        if result.returncode != 0:
            if is_official and is_repo_not_found(result.stderr):
                print(f"error: {plugin_type} '{name}' not found in {OFFICIAL_ORG} org")
            else:
                print(f"error: git clone failed: {result.stderr.strip()}")
            raise RuntimeError("git clone failed")

        # Mark as installing (prevents discovery during install)
        (plugin_dir / ".installing").touch()

        # Validate manifest
        toml_path = plugin_dir / "kiso.toml"
        if not toml_path.exists():
            print("error: kiso.toml not found in cloned repo")
            raise RuntimeError("missing kiso.toml")

        with open(toml_path, "rb") as f:
            manifest = tomllib.load(f)

        errors = validate_fn(manifest, plugin_dir)
        if errors:
            for e in errors:
                print(f"error: {e}")
            raise RuntimeError("manifest validation failed")

        # Unofficial repo warning
        if not is_official:
            print(f"WARNING: This is an unofficial {plugin_type} repo.")
            deps_path = plugin_dir / "deps.sh"
            if deps_path.exists():
                print("\ndeps.sh contents:")
                print(deps_path.read_text())
            answer = input("Continue? [y/N] ").strip().lower()
            if answer != "y":
                print("Installation cancelled.")
                raise RuntimeError("cancelled")

        # uv sync first (deps.sh may need packages installed by uv)
        subprocess.run(
            ["uv", "sync"],
            cwd=str(plugin_dir),
            capture_output=True, text=True,
        )

        # Run deps.sh if present and not --no-deps
        deps_path = plugin_dir / "deps.sh"
        if deps_path.exists() and not args.no_deps:
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: deps.sh failed: {result.stderr.strip()}")

        # Check binary deps — build proper info dict with deps from manifest
        kiso_section = manifest.get("kiso", {})
        plugin_info = {
            "path": str(plugin_dir),
            "deps": kiso_section.get("deps", {}),
        }
        missing = check_deps_fn(plugin_info)
        # M187: auto-retry deps.sh once if binaries still missing
        if missing and deps_path.exists() and not args.no_deps:
            print(f"Missing binaries: {', '.join(missing)} — re-running deps.sh...")
            retry_result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if retry_result.returncode != 0:
                print(f"warning: deps.sh retry failed: {retry_result.stderr.strip()}")
            missing = check_deps_fn(plugin_info)
        if missing:
            print(f"warning: still missing binaries after install: {', '.join(missing)}")
            print(f"  The {plugin_type} may not work correctly. Try: kiso {plugin_type} remove {name} && kiso {plugin_type} install {name}")
            print(f"  Or run deps.sh manually: bash {plugin_dir / 'deps.sh'}")

        # Type-specific post-install steps (env var check, config copy, etc.)
        if post_install is not None:
            post_install(manifest, plugin_dir, name)

        # Remove installing marker
        installing = plugin_dir / ".installing"
        if installing.exists():
            installing.unlink()

        print(f"{plugin_type.capitalize()} '{name}' installed successfully.")
        invalidate_cache()
        from kiso.tools import invalidate_tools_cache
        invalidate_tools_cache()

    except Exception:
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        sys.exit(1)


def _list_plugins(discover_fn, item_type: str) -> None:
    """List installed plugins (skills or connectors) in aligned columns."""
    items = discover_fn()
    if not items:
        print(f"No {item_type} installed.")
        return

    max_name = max(len(item["name"]) for item in items)
    max_ver = max(len(item["version"]) for item in items)
    for item in items:
        name = item["name"].ljust(max_name)
        ver = item["version"].ljust(max_ver)
        desc = item.get("summary", item.get("description", ""))
        print(f"  {name}  {ver}  — {desc}")
