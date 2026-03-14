"""Plugin umbrella CLI commands — unified view across tools, skills, connectors."""

from __future__ import annotations

import sys

from cli.plugin_ops import fetch_registry, search_entries
from kiso.connectors import discover_connectors
from kiso.skill_loader import discover_md_skills, invalidate_md_skills_cache
from kiso.tools import discover_tools


def run_plugin_command(args) -> None:
    """Dispatch to the appropriate plugin subcommand."""
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "plugin_command", {
        "list": lambda _: _plugin_list(), "search": _plugin_search,
    }, "usage: kiso plugin {list,search}")


def _plugin_list() -> None:
    """List all installed plugins grouped by type."""
    tools = discover_tools()
    invalidate_md_skills_cache()
    skills = discover_md_skills()
    connectors = discover_connectors()

    if not tools and not skills and not connectors:
        print("No plugins installed.")
        return

    for label, items in [("Tools", tools), ("Skills", skills), ("Connectors", connectors)]:
        if not items:
            continue
        print(f"{label}:")
        max_name = max(len(i["name"]) for i in items)
        for i in items:
            desc = i.get("description") or i.get("summary", "")
            print(f"  {i['name'].ljust(max_name)}  {desc}")
        print()


def _plugin_search(args) -> None:
    """Search registry across all plugin types."""
    registry = fetch_registry()
    query = getattr(args, "query", "")

    found_any = False
    for section, label in [("tools", "Tools"), ("skills", "Skills"), ("connectors", "Connectors")]:
        entries = registry.get(section, [])
        results = search_entries(entries, query)
        if results:
            found_any = True
            print(f"{label}:")
            max_name = max(len(r["name"]) for r in results)
            for r in results:
                print(f"  {r['name'].ljust(max_name)}  — {r.get('description', '')}")
            print()

    if not found_any:
        print("No plugins found.")
