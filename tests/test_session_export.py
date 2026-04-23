"""Unit tests for ``kiso/session_export.py`` — session export / import.

The business contract:

- ``pack_session`` writes a deterministic ``.tar.gz`` containing a
  ``manifest.json`` (kiso version + schema version + row counts), one
  ``.jsonl`` per exported table, and the full ``workspace/`` tree for
  that session.
- ``unpack_session`` round-trips: DB rows restored row-for-row (minus
  the auto-increment ids) and workspace files restored byte-for-byte.
- Import refuses an archive from a future schema version.
- ``--as <new_session_id>`` rewrites the session column during
  import so the same archive can be materialized multiple times.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def fixture_db(tmp_path: Path) -> sqlite3.Connection:
    from kiso.store.shared import SCHEMA

    conn = sqlite3.connect(tmp_path / "store.db")
    conn.executescript(SCHEMA)

    # Seed a session with representative rows across the exported
    # tables.
    conn.execute(
        "INSERT INTO sessions (session, description) VALUES (?, ?)",
        ("dev", "fixture session"),
    )
    conn.execute(
        "INSERT INTO messages (session, role, content) VALUES (?, ?, ?)",
        ("dev", "user", "hello"),
    )
    conn.execute(
        "INSERT INTO messages (session, role, content) VALUES (?, ?, ?)",
        ("dev", "assistant", "hi"),
    )
    conn.execute(
        "INSERT INTO plans (session, message_id, goal) VALUES (?, ?, ?)",
        ("dev", 1, "answer the user"),
    )
    conn.execute(
        "INSERT INTO tasks (plan_id, session, type, detail) "
        "VALUES (?, ?, ?, ?)",
        (1, "dev", "msg", "reply politely"),
    )
    conn.execute(
        "INSERT INTO facts (content, source, session) VALUES (?, ?, ?)",
        ("dev uses Python", "user", "dev"),
    )
    conn.execute(
        "INSERT INTO learnings (content, session) VALUES (?, ?)",
        ("polite greetings please", "dev"),
    )

    # Seed a second session whose rows must NOT appear in the export.
    conn.execute(
        "INSERT INTO sessions (session, description) VALUES (?, ?)",
        ("other", "unrelated"),
    )
    conn.execute(
        "INSERT INTO messages (session, role, content) VALUES (?, ?, ?)",
        ("other", "user", "do not export me"),
    )
    conn.commit()
    return conn


@pytest.fixture
def fixture_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "sessions" / "dev"
    (ws / "pub").mkdir(parents=True)
    (ws / "uploads").mkdir(parents=True)
    (ws / "notes.md").write_text("hello from the workspace\n")
    (ws / "pub" / "report.txt").write_text("published output\n")
    (ws / "uploads" / "input.csv").write_text("a,b,c\n1,2,3\n")
    return ws.parent  # parent holds many sessions; caller picks one


# ────────────────────────────────────────────────────────────────────
# pack_session
# ────────────────────────────────────────────────────────────────────

class TestPackSession:
    def test_creates_archive_with_manifest_and_jsonl(
        self, tmp_path: Path,
        fixture_db: sqlite3.Connection,
        fixture_workspace: Path,
    ) -> None:
        from kiso.session_export import pack_session

        out = tmp_path / "dev.kiso.tar.gz"
        pack_session(
            conn=fixture_db,
            session_id="dev",
            workspace_parent=fixture_workspace,
            output_path=out,
        )

        assert out.is_file()
        with tarfile.open(out, "r:gz") as tf:
            names = set(tf.getnames())
        assert "manifest.json" in names
        assert "messages.jsonl" in names
        assert "plans.jsonl" in names
        assert "tasks.jsonl" in names
        assert "facts.jsonl" in names
        assert "learnings.jsonl" in names
        assert any(n.startswith("workspace/") for n in names)

    def test_manifest_has_version_and_counts(
        self, tmp_path: Path,
        fixture_db: sqlite3.Connection,
        fixture_workspace: Path,
    ) -> None:
        from kiso.session_export import pack_session, SCHEMA_VERSION

        out = tmp_path / "dev.kiso.tar.gz"
        pack_session(
            conn=fixture_db,
            session_id="dev",
            workspace_parent=fixture_workspace,
            output_path=out,
        )

        with tarfile.open(out, "r:gz") as tf:
            manifest = json.loads(
                tf.extractfile("manifest.json").read().decode()
            )
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["session_id"] == "dev"
        assert manifest["kiso_version"]
        assert manifest["row_counts"]["messages"] == 2
        assert manifest["row_counts"]["plans"] == 1
        assert manifest["row_counts"]["tasks"] == 1
        assert manifest["row_counts"]["facts"] == 1
        assert manifest["row_counts"]["learnings"] == 1
        # Other session's rows excluded
        assert manifest["row_counts"]["sessions"] == 1

    def test_other_session_rows_excluded(
        self, tmp_path: Path,
        fixture_db: sqlite3.Connection,
        fixture_workspace: Path,
    ) -> None:
        from kiso.session_export import pack_session

        out = tmp_path / "dev.kiso.tar.gz"
        pack_session(
            conn=fixture_db,
            session_id="dev",
            workspace_parent=fixture_workspace,
            output_path=out,
        )

        with tarfile.open(out, "r:gz") as tf:
            msgs = [
                json.loads(line)
                for line in tf.extractfile(
                    "messages.jsonl"
                ).read().decode().splitlines()
            ]
        assert all(m["session"] == "dev" for m in msgs)
        assert all("do not export me" != m["content"] for m in msgs)


# ────────────────────────────────────────────────────────────────────
# unpack_session
# ────────────────────────────────────────────────────────────────────

class TestUnpackSession:
    def test_round_trip_restores_rows_and_files(
        self, tmp_path: Path,
        fixture_db: sqlite3.Connection,
        fixture_workspace: Path,
    ) -> None:
        from kiso.session_export import pack_session, unpack_session

        archive = tmp_path / "dev.kiso.tar.gz"
        pack_session(
            conn=fixture_db,
            session_id="dev",
            workspace_parent=fixture_workspace,
            output_path=archive,
        )

        # Fresh empty DB + empty workspace parent.
        from kiso.store.shared import SCHEMA

        target_db = sqlite3.connect(":memory:")
        target_db.executescript(SCHEMA)
        target_ws_parent = tmp_path / "restored_sessions"
        target_ws_parent.mkdir()

        unpack_session(
            archive_path=archive,
            conn=target_db,
            workspace_parent=target_ws_parent,
            as_session_id=None,
        )

        # Rows restored
        assert target_db.execute(
            "SELECT COUNT(*) FROM messages WHERE session = ?", ("dev",)
        ).fetchone()[0] == 2
        assert target_db.execute(
            "SELECT COUNT(*) FROM tasks WHERE session = ?", ("dev",)
        ).fetchone()[0] == 1
        # Files restored byte-for-byte
        restored_notes = (
            target_ws_parent / "dev" / "notes.md"
        ).read_text()
        assert restored_notes == "hello from the workspace\n"
        restored_report = (
            target_ws_parent / "dev" / "pub" / "report.txt"
        ).read_text()
        assert restored_report == "published output\n"

    def test_as_new_session_id_rewrites_rows(
        self, tmp_path: Path,
        fixture_db: sqlite3.Connection,
        fixture_workspace: Path,
    ) -> None:
        from kiso.session_export import pack_session, unpack_session

        archive = tmp_path / "dev.kiso.tar.gz"
        pack_session(
            conn=fixture_db,
            session_id="dev",
            workspace_parent=fixture_workspace,
            output_path=archive,
        )

        from kiso.store.shared import SCHEMA

        target_db = sqlite3.connect(":memory:")
        target_db.executescript(SCHEMA)
        target_ws_parent = tmp_path / "restored_sessions2"
        target_ws_parent.mkdir()

        unpack_session(
            archive_path=archive,
            conn=target_db,
            workspace_parent=target_ws_parent,
            as_session_id="archive1",
        )

        # Rows live under the new id, not the original.
        assert target_db.execute(
            "SELECT COUNT(*) FROM messages WHERE session = ?", ("dev",)
        ).fetchone()[0] == 0
        assert target_db.execute(
            "SELECT COUNT(*) FROM messages WHERE session = ?",
            ("archive1",),
        ).fetchone()[0] == 2
        assert (target_ws_parent / "archive1" / "notes.md").is_file()

    def test_rejects_future_schema_version(
        self, tmp_path: Path,
        fixture_db: sqlite3.Connection,
        fixture_workspace: Path,
    ) -> None:
        from kiso.session_export import (
            pack_session,
            unpack_session,
            SCHEMA_VERSION,
            SessionExportError,
        )

        archive = tmp_path / "dev.kiso.tar.gz"
        pack_session(
            conn=fixture_db,
            session_id="dev",
            workspace_parent=fixture_workspace,
            output_path=archive,
        )

        # Mutate the manifest schema_version to a future value.
        import gzip, io, tarfile as _tf
        with tarfile.open(archive, "r:gz") as src:
            members = [(m, src.extractfile(m).read()) for m in src.getmembers()]
        bumped = tmp_path / "future.kiso.tar.gz"
        with tarfile.open(bumped, "w:gz") as dst:
            for m, data in members:
                if m.name == "manifest.json":
                    mf = json.loads(data.decode())
                    mf["schema_version"] = SCHEMA_VERSION + 99
                    new = json.dumps(mf).encode()
                    m.size = len(new)
                    dst.addfile(m, io.BytesIO(new))
                else:
                    m.size = len(data)
                    dst.addfile(m, io.BytesIO(data))

        from kiso.store.shared import SCHEMA
        target_db = sqlite3.connect(":memory:")
        target_db.executescript(SCHEMA)
        target_ws_parent = tmp_path / "restored_sessions3"
        target_ws_parent.mkdir()

        with pytest.raises(SessionExportError):
            unpack_session(
                archive_path=bumped,
                conn=target_db,
                workspace_parent=target_ws_parent,
            )
