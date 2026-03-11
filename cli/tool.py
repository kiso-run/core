"""Tool management CLI commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from kiso.config import KISO_DIR
from cli.plugin_ops import (
    OFFICIAL_ORG,
    _GIT_ENV,
    _list_plugins,
    _plugin_install,
    cross_type_hint,
    fetch_registry,
    is_repo_not_found,
    is_url,
    require_admin,
    search_entries,
    url_to_name,
)
from kiso.tools import _env_var_name, _validate_manifest, check_deps, discover_tools

TOOLS_DIR = KISO_DIR / "tools"
OFFICIAL_PREFIX = "tool-"

# Backward-compatible aliases for cli_connector and tests that import from here
_is_url = is_url
_is_repo_not_found = is_repo_not_found
_require_admin = require_admin
_fetch_registry = fetch_registry
_search_entries = search_entries


def _tool_post_install(manifest: dict, tool_dir: Path, name: str) -> None:
    """Tool-specific post-install steps: env var warnings, usage guide, git exclude."""
    kiso_section = manifest.get("kiso", {})
    tool_section = kiso_section.get("tool", kiso_section.get("skill", {}))
    env_decl = tool_section.get("env", {})
    tool_name = kiso_section.get("name", name)
    for key in env_decl:
        var_name = _env_var_name(tool_name, key)
        if not os.environ.get(var_name):
            print(f"warning: {var_name} not set")

    # Create usage_guide.local.md from toml default if not already present
    usage_guide = tool_section.get("usage_guide", "")
    override_path = tool_dir / "usage_guide.local.md"
    if usage_guide and not override_path.exists():
        override_path.write_text(usage_guide + "\n")

    # Add usage_guide.local.md to .git/info/exclude so git pull won't conflict
    exclude_path = tool_dir / ".git" / "info" / "exclude"
    if exclude_path.exists():
        exclude_content = exclude_path.read_text()
        if "usage_guide.local.md" not in exclude_content:
            with open(exclude_path, "a") as f:
                f.write("\nusage_guide.local.md\n")


def run_tool_command(args) -> None:
    """Dispatch to the appropriate tool subcommand."""
    cmd = getattr(args, "tool_command", None)
    if cmd is None:
        print("usage: kiso tool {list,search,install,update,remove}")
        sys.exit(1)
    elif cmd == "list":
        _tool_list(args)
    elif cmd == "search":
        _tool_search(args)
    elif cmd == "install":
        _tool_install(args)
    elif cmd == "update":
        _tool_update(args)
    elif cmd == "remove":
        _tool_remove(args)


def _tool_list(args) -> None:
    """List installed tools."""
    _list_plugins(discover_tools, "tools")


def _tool_search(args) -> None:
    """Search official tools from the registry."""
    registry = _fetch_registry()
    # Accept both "tools" and legacy "skills" registry sections
    results = _search_entries(
        registry.get("tools", registry.get("skills", [])), args.query,
    )

    if not results:
        print("No tools found.")
        if args.query:
            hint = cross_type_hint(registry, "tools", args.query)
            if hint:
                print(hint)
        return

    max_name = max(len(r["name"]) for r in results)
    for r in results:
        print(f"  {r['name'].ljust(max_name)}  — {r.get('description', '')}")


def _tool_install(args) -> None:
    """Install a tool from official repo or git URL."""
    _require_admin()
    _plugin_install(
        plugin_type="tool",
        official_prefix=OFFICIAL_PREFIX,
        parent_dir=TOOLS_DIR,
        validate_fn=_validate_manifest,
        check_deps_fn=check_deps,
        args=args,
        post_install=_tool_post_install,
    )


def _tool_update(args) -> None:
    """Update an installed tool or all tools."""
    _require_admin()

    target = args.target
    if target == "all":
        if not TOOLS_DIR.is_dir():
            print("No tools installed.")
            return
        names = [d.name for d in sorted(TOOLS_DIR.iterdir()) if d.is_dir()]
        if not names:
            print("No tools installed.")
            return
    else:
        names = [target]

    for name in names:
        tool_dir = TOOLS_DIR / name
        if not tool_dir.exists():
            print(f"error: tool '{name}' is not installed")
            sys.exit(1)

        # git pull
        result = subprocess.run(
            ["git", "pull"],
            cwd=str(tool_dir),
            capture_output=True, text=True, env=_GIT_ENV,
        )
        if result.returncode != 0:
            print(f"error: git pull failed for '{name}': {result.stderr.strip()}")
            sys.exit(1)

        # uv sync first (deps.sh may need packages installed by uv)
        subprocess.run(
            ["uv", "sync"],
            cwd=str(tool_dir),
            capture_output=True, text=True,
        )

        # deps.sh
        deps_path = tool_dir / "deps.sh"
        if deps_path.exists():
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: deps.sh failed for '{name}': {result.stderr.strip()}")

        # check deps
        tool_info = {"path": str(tool_dir)}
        missing = check_deps(tool_info)
        if missing:
            print(f"warning: '{name}' missing binaries: {', '.join(missing)}")

        print(f"Tool '{name}' updated.")
        from kiso.sysenv import invalidate_cache
        invalidate_cache()


def _tool_remove(args) -> None:
    """Remove an installed tool."""
    _require_admin()

    name = args.name
    tool_dir = TOOLS_DIR / name
    if not tool_dir.exists():
        print(f"error: tool '{name}' is not installed")
        sys.exit(1)

    shutil.rmtree(tool_dir)
    print(f"Tool '{name}' removed.")
    from kiso.sysenv import invalidate_cache
    invalidate_cache()
    from kiso.tools import invalidate_tools_cache
    invalidate_tools_cache()
