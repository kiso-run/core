"""Session export / import — tarball round-trip.

A session's state is spread across SQLite tables (keyed by ``session``)
and a workspace directory (``~/.kiso/instances/<name>/sessions/<id>/``).
``pack_session`` collects both into a deterministic ``.tar.gz``;
``unpack_session`` restores them into a target DB + workspace parent.

The archive is self-describing via ``manifest.json`` (kiso version,
schema version, session id, per-table row counts). Import refuses an
archive from a future schema version — the receiver may not know how
to interpret new columns.

Keep this module dependency-free beyond the standard library so the
export can run from a clean checkout without the full runtime
imported. The CLI wrapper in ``cli/session.py`` is the user-facing
entry point.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Iterable, Sequence

from kiso._version import __version__ as _KISO_VERSION


# Bump when the export format changes in a non-backward-compatible way.
# The receiver will refuse archives with a higher version than this.
SCHEMA_VERSION = 1


# Tables exported per session. Order is stable across releases so
# archives remain byte-reproducible. "sessions" must come first so
# foreign references exist when the other rows are inserted.
_EXPORT_TABLES: tuple[tuple[str, str], ...] = (
    ("sessions", "session"),
    ("messages", "session"),
    ("plans", "session"),
    ("tasks", "session"),
    ("facts", "session"),
    ("learnings", "session"),
)


class SessionExportError(Exception):
    """Raised when an archive cannot be produced or consumed."""


def _rows_for_session(
    conn: sqlite3.Connection, table: str, session_col: str, session_id: str
) -> list[dict]:
    """Return the rows for *session_id* from *table* as plain dicts.

    ``session_col`` is the name of the column that scopes rows to a
    session — currently always ``"session"``, but threaded through for
    clarity and future-proofing.
    """
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE {session_col} = ? ORDER BY id",
        (session_id,),
    ) if _has_id_column(conn, table) else conn.execute(
        f"SELECT * FROM {table} WHERE {session_col} = ?",
        (session_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _has_id_column(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == "id" for r in cur.fetchall())


def _jsonl_bytes(rows: Iterable[dict]) -> bytes:
    # One JSON object per line, sorted keys so the byte output is
    # reproducible across runs.
    buf = io.BytesIO()
    for row in rows:
        buf.write(
            json.dumps(row, sort_keys=True, default=str).encode() + b"\n"
        )
    return buf.getvalue()


def _add_bytes(
    tf: tarfile.TarFile, name: str, data: bytes, *, mtime: float
) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(mtime)
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(data))


def _iter_workspace(root: Path) -> list[tuple[str, bytes]]:
    """Walk *root* recursively, returning (archive_name, bytes) pairs
    with a deterministic order. Symlinks and non-file entries are
    skipped — the workspace is treated as a blob of regular files.
    """
    out: list[tuple[str, bytes]] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        name = "workspace/" + p.relative_to(root).as_posix()
        out.append((name, p.read_bytes()))
    return out


def pack_session(
    *,
    conn: sqlite3.Connection,
    session_id: str,
    workspace_parent: Path,
    output_path: Path,
) -> None:
    """Write a ``.tar.gz`` capturing *session_id* into *output_path*.

    ``workspace_parent`` is the directory holding per-session
    subdirectories (e.g. ``~/.kiso/instances/<name>/sessions/``). The
    session's workspace is ``workspace_parent / session_id``.
    """
    workspace_root = Path(workspace_parent) / session_id
    mtime = time.time()

    table_rows: dict[str, list[dict]] = {}
    for table, col in _EXPORT_TABLES:
        table_rows[table] = _rows_for_session(conn, table, col, session_id)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kiso_version": _KISO_VERSION,
        "session_id": session_id,
        "exported_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)
        ),
        "row_counts": {t: len(table_rows[t]) for t, _ in _EXPORT_TABLES},
    }

    with tarfile.open(output_path, "w:gz") as tf:
        _add_bytes(
            tf,
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode(),
            mtime=mtime,
        )
        for table, _col in _EXPORT_TABLES:
            _add_bytes(
                tf,
                f"{table}.jsonl",
                _jsonl_bytes(table_rows[table]),
                mtime=mtime,
            )
        for name, data in _iter_workspace(workspace_root):
            _add_bytes(tf, name, data, mtime=mtime)


# ────────────────────────────────────────────────────────────────────
# Import
# ────────────────────────────────────────────────────────────────────

def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: Sequence[dict],
    *,
    rewrite_session: str | None,
    source_session: str,
) -> None:
    """Insert *rows* into *table*. ``id`` is dropped so SQLite assigns
    a fresh primary key. ``session`` is rewritten when the caller
    asked for ``--as <new_session_id>``."""
    if not rows:
        return
    cur = conn.execute(f"PRAGMA table_info({table})")
    table_cols = [r[1] for r in cur.fetchall()]
    keep = [c for c in table_cols if c in rows[0] and c != "id"]
    placeholders = ", ".join("?" for _ in keep)
    col_list = ", ".join(keep)
    stmt = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    for row in rows:
        values = []
        for c in keep:
            v = row.get(c)
            if c == "session" and rewrite_session and v == source_session:
                v = rewrite_session
            values.append(v)
        conn.execute(stmt, values)


def unpack_session(
    *,
    archive_path: Path,
    conn: sqlite3.Connection,
    workspace_parent: Path,
    as_session_id: str | None = None,
) -> dict:
    """Restore an archive into *conn* + *workspace_parent*.

    Returns the restored manifest dict (post-rewrite). Raises
    :class:`SessionExportError` if the archive's schema version is
    newer than this kiso build understands.
    """
    with tarfile.open(archive_path, "r:gz") as tf:
        manifest_f = tf.extractfile("manifest.json")
        if manifest_f is None:
            raise SessionExportError("archive is missing manifest.json")
        manifest = json.loads(manifest_f.read().decode())

        version = int(manifest.get("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise SessionExportError(
                f"archive schema_version={version} is newer than this "
                f"kiso build understands (max {SCHEMA_VERSION}). "
                f"Upgrade kiso to import this archive."
            )

        source_session = manifest["session_id"]
        target_session = as_session_id or source_session

        # Restore DB rows — sessions first, then the rest.
        for table, _col in _EXPORT_TABLES:
            jf = tf.extractfile(f"{table}.jsonl")
            if jf is None:
                continue
            raw = jf.read().decode()
            rows = [
                json.loads(line) for line in raw.splitlines() if line.strip()
            ]
            _insert_rows(
                conn,
                table,
                rows,
                rewrite_session=(
                    target_session if target_session != source_session
                    else None
                ),
                source_session=source_session,
            )
        conn.commit()

        # Restore the workspace tree.
        target_ws = Path(workspace_parent) / target_session
        target_ws.mkdir(parents=True, exist_ok=True)
        for member in tf.getmembers():
            if not member.name.startswith("workspace/") or member.isdir():
                continue
            data_f = tf.extractfile(member)
            if data_f is None:
                continue
            relative = member.name[len("workspace/"):]
            dest = target_ws / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data_f.read())

    manifest["session_id"] = target_session
    return manifest
