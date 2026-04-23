"""CLI commands for safety rules management."""

from __future__ import annotations

import argparse

from cli._http import cli_delete, cli_get, cli_post
from cli.render import die


def rules_list(args: argparse.Namespace) -> None:
    """List all safety rules."""
    resp = cli_get(args, "/safety-rules")
    data = resp.json()
    rules = data.get("rules", [])
    if not rules:
        print("No safety rules configured.")
        return
    for r in rules:
        print(f"  [{r['id']}] {r['content']}")


def rules_add(args: argparse.Namespace) -> None:
    """Add a safety rule."""
    from cli._admin import require_admin
    require_admin()
    content = args.rule_content
    if not content.strip():
        die("rule content cannot be empty")
    resp = cli_post(args, "/safety-rules", json_body={"content": content})
    data = resp.json()
    print(f"Safety rule added (id={data['id']}): {data['content']}")


def rules_remove(args: argparse.Namespace) -> None:
    """Remove a safety rule by ID."""
    from cli._admin import require_admin
    require_admin()
    resp = cli_delete(args, f"/safety-rules/{args.rule_id}")
    data = resp.json()
    if data.get("deleted"):
        print(f"Safety rule {args.rule_id} removed.")
    else:
        die(f"could not remove rule {args.rule_id}")
