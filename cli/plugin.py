"""Plugin umbrella CLI commands — unified view across wrappers and connectors."""

from __future__ import annotations

from cli.plugin_ops import fetch_registry, search_entries
from kiso.connectors import discover_connectors
from kiso.wrappers import discover_wrappers


def run_plugin_command(args) -> None:
    """Dispatch to the appropriate plugin subcommand."""
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "plugin_command", {
        "list": lambda _: _plugin_list(), "search": _plugin_search,
    }, "usage: kiso plugin {list,search}")


def _plugin_list() -> None:
    """List all installed plugins grouped by type."""
    wrappers = discover_wrappers()
    connectors = discover_connectors()

    if not wrappers and not connectors:
        print("No plugins installed.")
        return

    for label, items in [("Wrappers", wrappers), ("Connectors", connectors)]:
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
    for section, label in [("wrappers", "Wrappers"), ("connectors", "Connectors")]:
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
