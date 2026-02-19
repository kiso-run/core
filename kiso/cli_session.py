"""Session management CLI commands."""

from __future__ import annotations

import getpass
import sys
from datetime import datetime, timezone


def run_sessions_command(args) -> None:
    """List sessions from the kiso server."""
    import httpx

    from kiso.config import load_config

    cfg = load_config()
    token = cfg.tokens.get("cli")
    if not token:
        print("error: no 'cli' token in config.toml")
        sys.exit(1)

    user = getpass.getuser()
    api = args.api
    show_all = getattr(args, "show_all", False)

    try:
        resp = httpx.get(
            f"{api}/sessions",
            params={"user": user, "all": str(show_all).lower()},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        print(f"error: cannot connect to {api}")
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        print(f"error: {exc.response.status_code} — {exc.response.text}")
        sys.exit(1)

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
