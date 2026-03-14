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
    _update_plugin,
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
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "tool_command", {
        "list": _tool_list, "search": _tool_search, "install": _tool_install,
        "update": _tool_update, "remove": _tool_remove,
    }, "usage: kiso tool {list,search,install,update,remove}")


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
    from kiso.sysenv import invalidate_cache
    _update_plugin(
        args.target, TOOLS_DIR, "tool", check_deps,
        [invalidate_cache], uv_before_deps=True,
    )


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
