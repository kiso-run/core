"""Plugin umbrella CLI commands — unified view across connectors."""

from __future__ import annotations

from cli.plugin_ops import fetch_registry, search_entries
from kiso.connectors import discover_connectors


def run_plugin_command(args) -> None:
    """Dispatch to the appropriate plugin subcommand."""
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "plugin_command", {
        "list": lambda _: _plugin_list(), "search": _plugin_search,
    }, "usage: kiso plugin {list,search}")


def _plugin_list() -> None:
    """List all installed plugins."""
    connectors = discover_connectors()

    if not connectors:
        print("No plugins installed.")
        return

    print("Connectors:")
    max_name = max(len(i["name"]) for i in connectors)
    for i in connectors:
        desc = i.get("description") or i.get("summary", "")
        print(f"  {i['name'].ljust(max_name)}  {desc}")
    print()


def _plugin_search(args) -> None:
    """Search registry across all plugin types."""
    registry = fetch_registry()
    query = getattr(args, "query", "")

    entries = registry.get("connectors", [])
    results = search_entries(entries, query)
    if results:
        print("Connectors:")
        max_name = max(len(r["name"]) for r in results)
        for r in results:
            print(f"  {r['name'].ljust(max_name)}  — {r.get('description', '')}")
        print()
    else:
        print("No plugins found.")
