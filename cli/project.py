""": CLI commands for project management."""

from __future__ import annotations

import argparse
import sys

from cli._http import cli_delete, cli_get, cli_post


def project_list(args: argparse.Namespace) -> None:
    """List projects visible to the current user."""
    resp = cli_get(args, "/projects", params={"user": "admin"})
    projects = resp.json().get("projects", [])
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        desc = f"  — {p['description']}" if p.get("description") else ""
        print(f"  [{p['id']}] {p['name']}{desc}")


def project_create(args: argparse.Namespace) -> None:
    """Create a new project."""
    from cli.plugin_ops import require_admin
    require_admin()
    body: dict = {"name": args.name}
    if getattr(args, "description", None):
        body["description"] = args.description
    resp = cli_post(args, "/projects", json_body=body)
    data = resp.json()
    print(f"Project created: {data['name']} (id={data['id']})")


def project_show(args: argparse.Namespace) -> None:
    """Show project details."""
    resp = cli_get(args, f"/projects/{args.name}")
    data = resp.json()
    proj = data["project"]
    members = data.get("members", [])
    print(f"Project: {proj['name']}")
    if proj.get("description"):
        print(f"  Description: {proj['description']}")
    print(f"  Created by: {proj.get('created_by', 'unknown')}")
    print(f"  Members ({len(members)}):")
    for m in members:
        print(f"    {m['username']} ({m['role']})")


def project_bind(args: argparse.Namespace) -> None:
    """Bind a session to a project."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_post(args, f"/projects/{args.project}/bind/{args.session}")
    data = resp.json()
    if data.get("bound"):
        print(f"Session '{args.session}' bound to project '{args.project}'.")


def project_unbind(args: argparse.Namespace) -> None:
    """Unbind a session from its project."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_post(args, f"/projects/{args.project}/unbind/{args.session}")
    data = resp.json()
    if data.get("unbound"):
        print(f"Session '{args.session}' unbound from project '{args.project}'.")


def project_add_member(args: argparse.Namespace) -> None:
    """Add a member to a project."""
    from cli.plugin_ops import require_admin
    require_admin()
    body = {"username": args.username, "role": getattr(args, "role", "member") or "member"}
    resp = cli_post(args, f"/projects/{args.project}/members", json_body=body)
    data = resp.json()
    if data.get("added"):
        print(f"Added {data['username']} as {data['role']} to project '{args.project}'.")


def project_remove_member(args: argparse.Namespace) -> None:
    """Remove a member from a project."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_delete(args, f"/projects/{args.project}/members/{args.username}")
    data = resp.json()
    if data.get("removed"):
        print(f"Removed {args.username} from project '{args.project}'.")


def project_members(args: argparse.Namespace) -> None:
    """List members of a project."""
    resp = cli_get(args, f"/projects/{args.project}/members")
    members = resp.json().get("members", [])
    if not members:
        print("No members found.")
        return
    for m in members:
        print(f"  {m['username']} ({m['role']})")
