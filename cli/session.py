"""Session management CLI commands."""

from __future__ import annotations

import getpass
from datetime import datetime, timezone

from cli._http import cli_get, cli_post


def session_create(args) -> None:
    """: Create a named session."""
    from cli.plugin_ops import require_admin
    require_admin()
    body: dict = {"session": args.name}
    if getattr(args, "description", None):
        body["description"] = args.description
    resp = cli_post(args, "/sessions", json_body=body)
    data = resp.json()
    print(f"Session '{args.name}' created.")


def run_sessions_command(args) -> None:
    """List sessions from the kiso server."""
    user = getattr(args, "user", None) or getpass.getuser()
    show_all = args.show_all

    resp = cli_get(args, "/sessions", params={"user": user, "all": str(show_all).lower()})
    sessions = resp.json()
    if not sessions:
        print("No sessions found.")
        return

    max_name = max(len(s["session"]) for s in sessions)
    for s in sessions:
        name = s["session"].ljust(max_name)
        parts = []
        if s.get("connector"):
            parts.append(f"connector: {s['connector']}")
        parts.append(f"last activity: {_relative_time(s.get('updated_at'))}")
        print(f"  {name}  — {', '.join(parts)}")


def _relative_time(dt_str: str | None) -> str:
    """Convert a datetime string to a relative time like '2m ago'."""
    if not dt_str:
        return "unknown"

    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        seconds = int((now - dt).total_seconds())
    except (ValueError, TypeError):
        return "unknown"

    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m}m ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h}h ago"
    if seconds < 604800:
        d = seconds // 86400
        return f"{d}d ago"
    w = seconds // 604800
    return f"{w}w ago"
