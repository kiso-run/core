"""Reset / cleanup CLI commands."""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

from kiso.config import KISO_DIR
from cli.plugin_ops import require_admin

DB_PATH = KISO_DIR / "store.db"

# Tables that contain per-session data with a `session` column
_SESSION_TABLES = ("messages", "plans", "tasks", "facts", "learnings")

# The pending table uses `scope` instead of `session`
_SESSION_SCOPE_TABLE = "pending"

# All 7 user-data tables
_ALL_TABLES = ("sessions", "messages", "plans", "tasks", "facts", "learnings", "pending")

# Knowledge-only tables
_KNOWLEDGE_TABLES = ("facts", "learnings", "pending")


def _confirm(message: str, yes_flag: bool) -> bool:
    """Ask for confirmation. Returns True if confirmed."""
    if yes_flag:
        return True
    try:
        ans = input(f"{message} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans.strip().lower() in ("y", "yes")


def _open_db() -> sqlite3.Connection:
    """Open the store.db with WAL mode."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _reset_session(args) -> None:
    """Reset a single session: delete session data + workspace."""
    import getpass
    import socket

    name = args.name
    if name is None:
        name = f"{socket.gethostname()}@{getpass.getuser()}"

    if not _confirm(f"Reset session '{name}'? All session data will be deleted.", args.yes):
        print("Aborted.")
        return

    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}. Nothing to reset.")
        return

    conn = _open_db()
    try:
        for table in _SESSION_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE session = ?", (name,))  # noqa: S608
        conn.execute("DELETE FROM pending WHERE scope = ?", (name,))
        conn.execute("DELETE FROM sessions WHERE session = ?", (name,))
        conn.commit()
    finally:
        conn.close()

    # Remove workspace directory
    workspace = KISO_DIR / "sessions" / name
    if workspace.exists():
        shutil.rmtree(workspace)

    print(f"Session '{name}' reset.")


def _reset_knowledge(args) -> None:
    """Reset all knowledge: facts, learnings, pending."""
    if not _confirm("Reset all knowledge? Facts, learnings, and pending items will be deleted.", args.yes):
        print("Aborted.")
        return

    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}. Nothing to reset.")
        return

    conn = _open_db()
    try:
        for table in _KNOWLEDGE_TABLES:
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
        conn.commit()
    finally:
        conn.close()

    print("Knowledge reset.")


def _reset_all(args) -> None:
    """Reset all data: all DB tables + sessions/ + audit/ + .chat_history."""
    if not _confirm("Reset ALL data? All sessions, knowledge, audit, and history will be deleted.", args.yes):
        print("Aborted.")
        return

    if DB_PATH.exists():
        conn = _open_db()
        try:
            for table in _ALL_TABLES:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608
            conn.commit()
        finally:
            conn.close()

    # Filesystem cleanup
    for dirname in ("sessions", "audit"):
        d = KISO_DIR / dirname
        if d.exists():
            shutil.rmtree(d)

    chat_history = KISO_DIR / ".chat_history"
    if chat_history.exists():
        chat_history.unlink()

    print("All data reset.")


def _reset_factory(args) -> None:
    """Factory reset: delete store.db + all generated directories."""
    if not _confirm(
        "Factory reset? Everything except config.toml and .env will be deleted.",
        args.yes,
    ):
        print("Aborted.")
        return

    # Delete database
    if DB_PATH.exists():
        DB_PATH.unlink()

    # Delete directories
    for dirname in ("sessions", "audit", "skills", "connectors", "roles", "reference", "sys"):
        d = KISO_DIR / dirname
        if d.exists():
            shutil.rmtree(d)

    # Delete individual files
    for filename in (".chat_history", "server.log"):
        f = KISO_DIR / filename
        if f.exists():
            f.unlink()

    print("Factory reset complete. Restart the server to reinitialize.")


def run_reset_command(args) -> None:
    """Dispatch to the appropriate reset subcommand."""
    require_admin()

    cmd = getattr(args, "reset_command", None)
    if cmd is None:
        print("usage: kiso reset {session,knowledge,all,factory}")
        sys.exit(1)
    elif cmd == "session":
        _reset_session(args)
    elif cmd == "knowledge":
        _reset_knowledge(args)
    elif cmd == "all":
        _reset_all(args)
    elif cmd == "factory":
        _reset_factory(args)
