"""Tests for kiso.cli_reset — reset/cleanup commands."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.cli_reset import (
    _confirm,
    _reset_all,
    _reset_factory,
    _reset_knowledge,
    _reset_session,
    run_reset_command,
)
from kiso.store import SCHEMA


# ── Helpers ──────────────────────────────────────────────


def _make_args(reset_command=None, yes=True, name=None, **kwargs):
    ns = {"reset_command": reset_command, "yes": yes, "name": name, "command": "reset"}
    ns.update(kwargs)
    return argparse.Namespace(**ns)


def _mock_admin():
    """Patch require_admin to be a no-op."""
    return patch("kiso.cli_reset.require_admin")


def _create_test_db(db_path: Path) -> sqlite3.Connection:
    """Create a test database with the full schema and return the connection."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _seed_session(conn: sqlite3.Connection, session: str = "test-session") -> None:
    """Insert test data for a session across all relevant tables."""
    conn.execute("INSERT INTO sessions (session) VALUES (?)", (session,))
    conn.execute(
        "INSERT INTO messages (session, role, content) VALUES (?, 'user', 'hello')",
        (session,),
    )
    conn.execute(
        "INSERT INTO plans (session, message_id, goal) VALUES (?, 1, 'test goal')",
        (session,),
    )
    conn.execute(
        "INSERT INTO tasks (plan_id, session, type, detail) VALUES (1, ?, 'msg', 'test')",
        (session,),
    )
    conn.execute(
        "INSERT INTO facts (content, source, session) VALUES ('fact1', 'curator', ?)",
        (session,),
    )
    conn.execute(
        "INSERT INTO learnings (content, session) VALUES ('learning1', ?)",
        (session,),
    )
    conn.execute(
        "INSERT INTO pending (content, scope, source) VALUES ('pending1', ?, 'curator')",
        (session,),
    )
    conn.commit()


def _count(conn: sqlite3.Connection, table: str, session: str | None = None) -> int:
    if session:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE session = ?", (session,))
    else:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


# ── _confirm ─────────────────────────────────────────────


class TestConfirm:
    def test_yes_flag_skips_prompt(self):
        assert _confirm("Delete?", yes_flag=True) is True

    def test_declined(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert _confirm("Delete?", yes_flag=False) is False

    def test_accepted(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert _confirm("Delete?", yes_flag=False) is True

    def test_empty_input_declines(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _confirm("Delete?", yes_flag=False) is False

    def test_eof_declines(self, monkeypatch):
        def raise_eof(_):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        assert _confirm("Delete?", yes_flag=False) is False

    def test_keyboard_interrupt_declines(self, monkeypatch):
        def raise_kb(_):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", raise_kb)
        assert _confirm("Delete?", yes_flag=False) is False

    def test_yes_word_accepts(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "yes")
        assert _confirm("Delete?", yes_flag=False) is True


# ── reset session ────────────────────────────────────────


class TestResetSession:
    def test_deletes_session_data(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "mysession")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_session(_make_args(reset_command="session", name="mysession"))

        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "sessions", "mysession") == 0
        assert _count(conn, "messages", "mysession") == 0
        assert _count(conn, "plans", "mysession") == 0
        assert _count(conn, "tasks", "mysession") == 0
        assert _count(conn, "facts") == 0  # facts have session column
        assert _count(conn, "learnings") == 0
        conn.close()
        assert "reset" in capsys.readouterr().out.lower()

    def test_default_session_name(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        default_name = "myhost@myuser"
        _seed_session(conn, default_name)
        conn.close()

        with (
            patch("kiso.cli_reset.DB_PATH", db_path),
            patch("kiso.cli_reset.KISO_DIR", tmp_path),
            patch("socket.gethostname", return_value="myhost"),
            patch("getpass.getuser", return_value="myuser"),
        ):
            _reset_session(_make_args(reset_command="session", name=None))

        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "sessions", default_name) == 0
        conn.close()

    def test_deletes_workspace(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "ws-test")
        conn.close()

        workspace = tmp_path / "sessions" / "ws-test"
        workspace.mkdir(parents=True)
        (workspace / "file.txt").write_text("data")

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_session(_make_args(reset_command="session", name="ws-test"))

        assert not workspace.exists()

    def test_nonexistent_session_graceful(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_session(_make_args(reset_command="session", name="nonexistent"))

        assert "reset" in capsys.readouterr().out.lower()

    def test_no_db_graceful(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        # No DB created

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_session(_make_args(reset_command="session", name="any"))

        assert "no database" in capsys.readouterr().out.lower()

    def test_other_sessions_preserved(self, tmp_path, capsys):
        """Resetting one session must not affect another."""
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "delete-me")
        _seed_session(conn, "keep-me")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_session(_make_args(reset_command="session", name="delete-me"))

        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "sessions", "delete-me") == 0
        assert _count(conn, "sessions", "keep-me") == 1
        assert _count(conn, "messages", "keep-me") == 1
        assert _count(conn, "plans", "keep-me") == 1
        assert _count(conn, "tasks", "keep-me") == 1
        conn.close()

    def test_pending_scope_cleared(self, tmp_path, capsys):
        """Pending items scoped to the session are cleared via scope column."""
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "target")
        # Add an extra pending item scoped to a different session
        conn.execute(
            "INSERT INTO pending (content, scope, source) VALUES ('other', 'other-session', 'curator')"
        )
        conn.commit()
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_session(_make_args(reset_command="session", name="target"))

        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("SELECT COUNT(*) FROM pending WHERE scope = 'target'")
        assert cur.fetchone()[0] == 0
        cur = conn.execute("SELECT COUNT(*) FROM pending WHERE scope = 'other-session'")
        assert cur.fetchone()[0] == 1
        conn.close()


# ── reset knowledge ──────────────────────────────────────


class TestResetKnowledge:
    def test_deletes_all_knowledge(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "s1")
        _seed_session(conn, "s2")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path):
            _reset_knowledge(_make_args(reset_command="knowledge"))

        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "facts") == 0
        assert _count(conn, "learnings") == 0
        assert _count(conn, "pending") == 0
        conn.close()

    def test_keeps_sessions(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "keep-me")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path):
            _reset_knowledge(_make_args(reset_command="knowledge"))

        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "sessions") == 1
        assert _count(conn, "messages") == 1
        assert _count(conn, "plans") == 1
        assert _count(conn, "tasks") == 1
        conn.close()

    def test_no_db_graceful(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"

        with patch("kiso.cli_reset.DB_PATH", db_path):
            _reset_knowledge(_make_args(reset_command="knowledge"))

        assert "no database" in capsys.readouterr().out.lower()

    def test_aborted(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "s1")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path):
            _reset_knowledge(_make_args(reset_command="knowledge", yes=False))

        assert "aborted" in capsys.readouterr().out.lower()
        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "facts") == 1
        conn.close()


# ── reset all ────────────────────────────────────────────


class TestResetAll:
    def test_clears_everything(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "s1")
        conn.close()

        # Create filesystem artifacts
        (tmp_path / "sessions" / "s1").mkdir(parents=True)
        (tmp_path / "audit").mkdir(parents=True)
        (tmp_path / "audit" / "2024-01-01.jsonl").write_text("{}")
        (tmp_path / ".chat_history").write_text("history")

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_all(_make_args(reset_command="all"))

        conn = sqlite3.connect(str(db_path))
        for table in ("sessions", "messages", "plans", "tasks", "facts", "learnings", "pending"):
            assert _count(conn, table) == 0, f"{table} should be empty"
        conn.close()

        assert not (tmp_path / "sessions").exists()
        assert not (tmp_path / "audit").exists()
        assert not (tmp_path / ".chat_history").exists()

    def test_keeps_config(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        (tmp_path / "config.toml").write_text("[tokens]\ncli = 'tok'\n")
        (tmp_path / ".env").write_text("KEY=val\n")
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "search").mkdir()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_all(_make_args(reset_command="all"))

        assert (tmp_path / "config.toml").exists()
        assert (tmp_path / ".env").exists()
        assert (tmp_path / "skills").exists()

    def test_preserves_db_file(self, tmp_path, capsys):
        """reset all clears rows but keeps store.db (unlike factory)."""
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "s1")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_all(_make_args(reset_command="all"))

        assert db_path.exists()

    def test_no_db_graceful(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_all(_make_args(reset_command="all"))

        assert "all data reset" in capsys.readouterr().out.lower()

    def test_aborted(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "s1")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_all(_make_args(reset_command="all", yes=False))

        assert "aborted" in capsys.readouterr().out.lower()
        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "sessions") == 1
        conn.close()


# ── reset factory ────────────────────────────────────────


class TestResetFactory:
    def test_deletes_db(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()
        assert db_path.exists()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_factory(_make_args(reset_command="factory"))

        assert not db_path.exists()
        assert "factory reset complete" in capsys.readouterr().out.lower()

    def test_keeps_config(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        (tmp_path / "config.toml").write_text("[tokens]\ncli = 'tok'\n")
        (tmp_path / ".env").write_text("KEY=val\n")
        (tmp_path / "docker-compose.yml").write_text("services:\n")

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_factory(_make_args(reset_command="factory"))

        assert (tmp_path / "config.toml").exists()
        assert (tmp_path / ".env").exists()
        assert (tmp_path / "docker-compose.yml").exists()

    def test_deletes_plugins(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        for dirname in ("skills", "connectors", "roles", "reference", "sys"):
            d = tmp_path / dirname
            d.mkdir(parents=True)
            (d / "test.txt").write_text("data")

        (tmp_path / "sessions").mkdir()
        (tmp_path / "audit").mkdir()
        (tmp_path / ".chat_history").write_text("history")
        (tmp_path / "server.log").write_text("logs")

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_factory(_make_args(reset_command="factory"))

        for dirname in ("sessions", "audit", "skills", "connectors", "roles", "reference", "sys"):
            assert not (tmp_path / dirname).exists(), f"{dirname}/ should be deleted"
        assert not (tmp_path / ".chat_history").exists()
        assert not (tmp_path / "server.log").exists()

    def test_no_db_graceful(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        # No DB — should not error
        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_factory(_make_args(reset_command="factory"))

        assert "factory reset complete" in capsys.readouterr().out.lower()


# ── run_reset_command dispatch ───────────────────────────


class TestRunResetCommand:
    def test_no_subcommand_exits(self, capsys):
        with _mock_admin(), pytest.raises(SystemExit, match="1"):
            run_reset_command(_make_args(reset_command=None))
        assert "usage:" in capsys.readouterr().out

    def test_requires_admin(self):
        with (
            patch("kiso.cli_reset.require_admin", side_effect=SystemExit(1)),
            pytest.raises(SystemExit, match="1"),
        ):
            run_reset_command(_make_args(reset_command="knowledge"))

    def test_dispatches_session(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        with (
            _mock_admin(),
            patch("kiso.cli_reset.DB_PATH", db_path),
            patch("kiso.cli_reset.KISO_DIR", tmp_path),
        ):
            run_reset_command(_make_args(reset_command="session", name="any"))

    def test_dispatches_knowledge(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        with _mock_admin(), patch("kiso.cli_reset.DB_PATH", db_path):
            run_reset_command(_make_args(reset_command="knowledge"))

    def test_dispatches_all(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        with (
            _mock_admin(),
            patch("kiso.cli_reset.DB_PATH", db_path),
            patch("kiso.cli_reset.KISO_DIR", tmp_path),
        ):
            run_reset_command(_make_args(reset_command="all"))

    def test_dispatches_factory(self, tmp_path, capsys):
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        with (
            _mock_admin(),
            patch("kiso.cli_reset.DB_PATH", db_path),
            patch("kiso.cli_reset.KISO_DIR", tmp_path),
        ):
            run_reset_command(_make_args(reset_command="factory"))


# ── Confirmation aborts ─────────────────────────────────


class TestConfirmationAborts:
    def test_session_aborted(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        _seed_session(conn, "keep")
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_session(_make_args(reset_command="session", name="keep", yes=False))

        assert "aborted" in capsys.readouterr().out.lower()
        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "sessions") == 1
        conn.close()

    def test_factory_aborted(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        db_path = tmp_path / "store.db"
        conn = _create_test_db(db_path)
        conn.close()

        with patch("kiso.cli_reset.DB_PATH", db_path), patch("kiso.cli_reset.KISO_DIR", tmp_path):
            _reset_factory(_make_args(reset_command="factory", yes=False))

        assert "aborted" in capsys.readouterr().out.lower()
        assert db_path.exists()
