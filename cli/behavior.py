"""M674: CLI commands for behavior management (convenience wrapper over /knowledge)."""

from __future__ import annotations

import argparse
import sys

from cli._http import cli_delete, cli_get, cli_post


def behavior_list(args: argparse.Namespace) -> None:
    """List all behavioral guidelines."""
    resp = cli_get(args, "/knowledge", params={"category": "behavior"})
    facts = resp.json().get("facts", [])
    if not facts:
        print("No behavioral guidelines configured.")
        return
    for f in facts:
        print(f"  [{f['id']}] {f['content']}")


def behavior_add(args: argparse.Namespace) -> None:
    """Add a behavioral guideline."""
    from cli.plugin_ops import require_admin
    require_admin()
    content = args.content
    if not content.strip():
        print("error: content cannot be empty", file=sys.stderr)
        sys.exit(1)
    resp = cli_post(args, "/knowledge", json_body={
        "content": content,
        "category": "behavior",
    })
    data = resp.json()
    print(f"Behavior added (id={data['id']}): {data['content']}")


def behavior_remove(args: argparse.Namespace) -> None:
    """Remove a behavioral guideline by ID."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_delete(args, f"/knowledge/{args.behavior_id}",
                      params={"expected_category": "behavior"})
    data = resp.json()
    if data.get("deleted"):
        print(f"Behavior {args.behavior_id} removed.")
    else:
        print(f"error: could not remove behavior {args.behavior_id}", file=sys.stderr)
        sys.exit(1)
