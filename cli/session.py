"""Session management CLI commands."""

from __future__ import annotations

import getpass
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from cli._http import cli_get, cli_post


def session_create(args) -> None:
    """: Create a named session."""
    from cli._admin import require_admin
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


def _kiso_paths() -> tuple[Path, Path]:
    """Resolve (store.db path, sessions parent dir) from the active instance."""
    from kiso.config import KISO_DIR

    return KISO_DIR / "store.db", KISO_DIR / "sessions"


def session_export(args) -> int:
    """``kiso session export <id> [--output <file>]``."""
    from kiso.session_export import pack_session, SessionExportError

    db_path, ws_parent = _kiso_paths()
    if not db_path.is_file():
        print(f"error: store database not found: {db_path}", file=sys.stderr)
        return 2

    output = args.output
    if not output:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        output = f"{args.session_id}-{today}.kiso.tar.gz"

    conn = sqlite3.connect(db_path)
    try:
        pack_session(
            conn=conn,
            session_id=args.session_id,
            workspace_parent=ws_parent,
            output_path=Path(output),
        )
    except SessionExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"Exported session '{args.session_id}' → {output}")
    return 0


def session_import(args) -> int:
    """``kiso session import <file> [--as <new_id>]``."""
    from kiso.session_export import unpack_session, SessionExportError

    archive = Path(args.archive)
    if not archive.is_file():
        print(f"error: archive not found: {archive}", file=sys.stderr)
        return 2

    db_path, ws_parent = _kiso_paths()
    ws_parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        manifest = unpack_session(
            archive_path=archive,
            conn=conn,
            workspace_parent=ws_parent,
            as_session_id=args.as_session_id,
        )
    except SessionExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(
        f"Imported session '{manifest['session_id']}' "
        f"(schema v{manifest['schema_version']}, "
        f"kiso {manifest['kiso_version']})"
    )
    return 0


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
