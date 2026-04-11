"""Shared utilities for tool and connector CLI operations."""

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
from cli.render import die


def _clean_env() -> dict[str, str]:
    """Build a subprocess environment without VIRTUAL_ENV.

    When running inside a kiso container, the parent process has
    VIRTUAL_ENV=/opt/kiso/.venv. Tool deps.sh and uv sync need to
    operate on the tool's own .venv, not kiso's. Removing VIRTUAL_ENV
    prevents uv from getting confused.
    """
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    return env
from kiso.registry import (
    cross_type_hint,
    fetch_registry as _fetch_registry_core,
    search_entries,
)

OFFICIAL_ORG = "kiso-run"

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
    """Fetch the official registry — exits on failure (CLI use).

    For core/worker code that must not exit, use
    ``kiso.registry.fetch_registry()`` directly.
    """
    reg = _fetch_registry_core()
    if not reg:
        print("error: failed to fetch registry")
        sys.exit(1)
    return reg


def _plugin_install(
    plugin_type: str,
    official_prefix: str,
    parent_dir: Path,
    validate_fn,
    check_deps_fn,
    args,
    post_install=None,
) -> None:
    """Shared install logic for tools and connectors.

    Args:
        plugin_type: "tool" or "connector" — used in user-facing messages.
        official_prefix: Git repo name prefix ("tool-" or "connector-").
        parent_dir: Directory where the plugin is installed (WRAPPERS_DIR/CONNECTORS_DIR).
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
        # Already installed — update source code and refresh deps
        print(f"{plugin_type.capitalize()} '{name}' is already installed — refreshing deps...")
        try:
            # M980: git pull to update source code (not just deps)
            # M982: pass safe.directory to avoid "dubious ownership" errors
            #       when kiso runs as a different user than the plugin owner.
            result = subprocess.run(
                ["git", "-c", f"safe.directory={plugin_dir}", "pull", "--ff-only"],
                cwd=str(plugin_dir), capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: git pull failed for '{name}': {result.stderr.strip()}")
            subprocess.run(["uv", "sync"], cwd=str(plugin_dir), capture_output=True, text=True, env=_clean_env())
            deps_path = plugin_dir / "deps.sh"
            if deps_path.exists() and not args.no_deps:
                result = subprocess.run(["bash", str(deps_path)], capture_output=True, text=True, env=_clean_env())
                if result.returncode != 0:
                    print(f"warning: deps.sh failed: {result.stderr.strip()}")
            # Check binary deps
            toml_path = plugin_dir / "kiso.toml"
            if toml_path.exists():
                with open(toml_path, "rb") as f:
                    manifest = tomllib.load(f)
                kiso_section = manifest.get("kiso", {})
                plugin_info = {"path": str(plugin_dir), "deps": kiso_section.get("deps", {})}
                missing = check_deps_fn(plugin_info)
                if missing:
                    print(f"warning: still missing binaries: {', '.join(missing)}")
                else:
                    print(f"  {plugin_type} '{name}' is healthy")
        except Exception as e:
            print(f"warning: deps refresh failed: {e}")
        return

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
            capture_output=True, text=True, env=_clean_env(),
        )

        # Run deps.sh if present and not --no-deps
        deps_path = plugin_dir / "deps.sh"
        if deps_path.exists() and not args.no_deps:
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True, env=_clean_env(),
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
        # auto-retry deps.sh once if binaries still missing
        if missing and deps_path.exists() and not args.no_deps:
            print(f"Missing binaries: {', '.join(missing)} — re-running deps.sh...")
            retry_result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True, env=_clean_env(),
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
        from kiso.wrappers import invalidate_wrappers_cache
        invalidate_wrappers_cache()

    except Exception:
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        sys.exit(1)


def render_aligned_list(
    items: list[dict],
    name_key: str,
    desc_key: str | None = None,
    desc_fallback: str | None = None,
    extra_cols: list[str] | None = None,
) -> None:
    """Print items as aligned columns: name [extra_cols...] [— description].

    Args:
        items: List of dicts to render (must be non-empty).
        name_key: Dict key for primary column.
        desc_key: Optional dict key for description (after "—").
        desc_fallback: Fallback key if desc_key is empty/missing.
        extra_cols: Optional list of dict keys for columns between name and desc.
    """
    if not items:
        return
    max_name = max(len(str(i[name_key])) for i in items)
    max_extras: dict[str, int] = {}
    for col in extra_cols or []:
        max_extras[col] = max(len(str(i.get(col, ""))) for i in items)
    for item in items:
        parts = [f"  {str(item[name_key]).ljust(max_name)}"]
        for col in extra_cols or []:
            parts.append(str(item.get(col, "")).ljust(max_extras[col]))
        if desc_key:
            desc = item.get(desc_key, "")
            if not desc and desc_fallback:
                desc = item.get(desc_fallback, "")
            parts.append(f"— {desc}")
        print("  ".join(parts))


def _list_plugins(discover_fn, item_type: str) -> None:
    """List installed plugins (tools or connectors) in aligned columns."""
    items = discover_fn()
    if not items:
        print(f"No {item_type} installed.")
        return
    render_aligned_list(items, "name", "summary", "description", extra_cols=["version"])


def _check_plugin_installed(plugin_dir: Path, plugin_type: str, name: str) -> None:
    """Exit with error if the plugin directory does not exist."""
    if not plugin_dir.exists():
        die(f"{plugin_type} '{name}' is not installed")


def _render_search_results(
    results: list[dict], query: str, plugin_type: str, registry: dict,
) -> None:
    """Render search results or a 'not found' message with cross-type hint."""
    if not results:
        print(f"No {plugin_type}s found.")
        if query:
            hint = cross_type_hint(registry, plugin_type + "s", query)
            if hint:
                print(hint)
        return
    render_aligned_list(results, "name", "description")


def _remove_plugin(
    name: str, plugin_dir: Path, plugin_type: str,
    cache_invalidators: list,
) -> None:
    """Remove an installed plugin directory and invalidate caches."""
    _check_plugin_installed(plugin_dir, plugin_type, name)
    shutil.rmtree(plugin_dir)
    print(f"{plugin_type.capitalize()} '{name}' removed.")
    for fn in cache_invalidators:
        fn()


def _update_plugin(
    target: str, plugin_dir: Path, plugin_type: str,
    check_deps_fn, cache_invalidators: list,
    *, uv_before_deps: bool = True,
) -> None:
    """Update one or all plugins of a given type.

    *uv_before_deps*: if True, run ``uv sync`` before ``deps.sh`` (tools);
    if False, run ``deps.sh`` first (connectors).
    """
    if target == "all":
        if not plugin_dir.is_dir():
            print(f"No {plugin_type}s installed.")
            return
        names = [d.name for d in sorted(plugin_dir.iterdir()) if d.is_dir()]
        if not names:
            print(f"No {plugin_type}s installed.")
            return
    else:
        names = [target]

    for name in names:
        item_dir = plugin_dir / name
        if not item_dir.exists():
            print(f"error: {plugin_type} '{name}' is not installed")
            sys.exit(1)

        # git pull
        result = subprocess.run(
            ["git", "pull"], cwd=str(item_dir),
            capture_output=True, text=True, env=_GIT_ENV,
        )
        if result.returncode != 0:
            print(f"error: git pull failed for '{name}': {result.stderr.strip()}")
            sys.exit(1)

        deps_path = item_dir / "deps.sh"

        if uv_before_deps:
            subprocess.run(["uv", "sync"], cwd=str(item_dir), capture_output=True, text=True, env=_clean_env())
            if deps_path.exists():
                r = subprocess.run(["bash", str(deps_path)], capture_output=True, text=True, env=_clean_env())
                if r.returncode != 0:
                    print(f"warning: deps.sh failed for '{name}': {r.stderr.strip()}")
        else:
            if deps_path.exists():
                r = subprocess.run(["bash", str(deps_path)], capture_output=True, text=True, env=_clean_env())
                if r.returncode != 0:
                    print(f"warning: deps.sh failed for '{name}': {r.stderr.strip()}")
            subprocess.run(["uv", "sync"], cwd=str(item_dir), capture_output=True, text=True, env=_clean_env())

        info = {"path": str(item_dir)}
        missing = check_deps_fn(info)
        if missing:
            print(f"warning: '{name}' missing binaries: {', '.join(missing)}")

        print(f"{plugin_type.capitalize()} '{name}' updated.")
        for fn in cache_invalidators:
            fn()


def dispatch_subcommand(
    args: object, attr: str, handlers: dict, usage: str,
) -> None:
    """Dispatch a CLI subcommand to its handler.

    Reads ``getattr(args, attr)`` and calls the matching handler.
    Falls back to printing *usage* and exiting with code 1.
    """
    from collections.abc import Callable

    cmd = getattr(args, attr, None)
    if cmd is None or cmd not in handlers:
        print(usage)
        sys.exit(1)
    handlers[cmd](args)
