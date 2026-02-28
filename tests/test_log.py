"""Tests for kiso/log.py — server and session logging."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.log import SessionLogger, setup_logging


@pytest.fixture(autouse=True)
def _clean_loggers():
    """Remove handlers added during tests so they don't leak."""
    yield
    # Clean up kiso root logger
    root = logging.getLogger("kiso")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    # Clean up any session loggers
    manager = logging.Logger.manager
    to_remove = [n for n in manager.loggerDict if n.startswith("kiso.session.")]
    for name in to_remove:
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()


# --- setup_logging ---


class TestSetupLogging:
    def test_creates_server_log_file(self, tmp_path):
        with patch("kiso.log.KISO_DIR", tmp_path):
            setup_logging()

        assert (tmp_path / "server.log").exists()

    def test_attaches_two_handlers(self, tmp_path):
        with patch("kiso.log.KISO_DIR", tmp_path):
            setup_logging()

        root = logging.getLogger("kiso")
        assert len(root.handlers) == 2

    def test_idempotent(self, tmp_path):
        with patch("kiso.log.KISO_DIR", tmp_path):
            setup_logging()
            setup_logging()

        root = logging.getLogger("kiso")
        assert len(root.handlers) == 2  # not 4

    def test_child_logger_writes_to_server_log(self, tmp_path):
        with patch("kiso.log.KISO_DIR", tmp_path):
            setup_logging()

        child = logging.getLogger("kiso.test_child")
        child.info("hello from child")

        content = (tmp_path / "server.log").read_text()
        assert "hello from child" in content

    def test_log_format(self, tmp_path):
        with patch("kiso.log.KISO_DIR", tmp_path):
            setup_logging()

        child = logging.getLogger("kiso.test_fmt")
        child.info("format check")

        content = (tmp_path / "server.log").read_text()
        assert "[kiso.test_fmt]" in content
        assert "INFO" in content

    def test_sets_log_level(self, tmp_path):
        with patch("kiso.log.KISO_DIR", tmp_path):
            setup_logging(level=logging.WARNING)

        root = logging.getLogger("kiso")
        assert root.level == logging.WARNING

    def test_creates_kiso_dir_if_missing(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        with patch("kiso.log.KISO_DIR", nested):
            setup_logging()

        assert (nested / "server.log").exists()


# --- SessionLogger ---


class TestSessionLogger:
    def test_creates_session_log_file(self, tmp_path):
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        slog.info("hello")
        slog.close()

        assert (tmp_path / "sessions" / "test-sess" / "session.log").exists()

    def test_writes_info(self, tmp_path):
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        slog.info("plan created: %d tasks", 5)
        slog.close()

        content = (tmp_path / "sessions" / "test-sess" / "session.log").read_text()
        assert "plan created: 5 tasks" in content

    def test_writes_warning(self, tmp_path):
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        slog.warning("something wrong")
        slog.close()

        content = (tmp_path / "sessions" / "test-sess" / "session.log").read_text()
        assert "WARNING" in content
        assert "something wrong" in content

    def test_writes_error(self, tmp_path):
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        slog.error("task failed: %s", "timeout")
        slog.close()

        content = (tmp_path / "sessions" / "test-sess" / "session.log").read_text()
        assert "ERROR" in content
        assert "task failed: timeout" in content

    def test_does_not_propagate(self, tmp_path):
        """Session log entries should not appear in the server log."""
        with patch("kiso.log.KISO_DIR", tmp_path):
            setup_logging()

        slog = SessionLogger("test-sess", base_dir=tmp_path)
        slog.info("session only message")
        slog.close()

        server_content = (tmp_path / "server.log").read_text()
        assert "session only message" not in server_content

    def test_close_removes_handler(self, tmp_path):
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        logger = logging.getLogger(slog._name)
        assert len(logger.handlers) == 1
        slog.close()
        assert len(logger.handlers) == 0

    def test_log_format_includes_session_name(self, tmp_path):
        slog = SessionLogger("my-session", base_dir=tmp_path)
        slog.info("check format")
        slog.close()

        content = (tmp_path / "sessions" / "my-session" / "session.log").read_text()
        assert "[kiso.session.my-session]" in content
        assert "INFO" in content

    def test_multiple_sessions_independent(self, tmp_path):
        slog1 = SessionLogger("sess-a", base_dir=tmp_path)
        slog2 = SessionLogger("sess-b", base_dir=tmp_path)

        slog1.info("message for A")
        slog2.info("message for B")

        slog1.close()
        slog2.close()

        content_a = (tmp_path / "sessions" / "sess-a" / "session.log").read_text()
        content_b = (tmp_path / "sessions" / "sess-b" / "session.log").read_text()

        assert "message for A" in content_a
        assert "message for B" not in content_a
        assert "message for B" in content_b
        assert "message for A" not in content_b

    def test_creates_session_dir_if_missing(self, tmp_path):
        slog = SessionLogger("new-sess", base_dir=tmp_path)
        slog.info("first message")
        slog.close()

        assert (tmp_path / "sessions" / "new-sess").is_dir()

    def test_writes_debug(self, tmp_path):
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        slog.debug("debug detail: %s", "value")
        slog.close()

        content = (tmp_path / "sessions" / "test-sess" / "session.log").read_text()
        assert "DEBUG" in content
        assert "debug detail: value" in content

    def test_writes_exception(self, tmp_path):
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        try:
            raise ValueError("boom")
        except ValueError:
            slog.exception("caught exception")
        slog.close()

        content = (tmp_path / "sessions" / "test-sess" / "session.log").read_text()
        assert "ERROR" in content
        assert "caught exception" in content
        assert "ValueError" in content  # traceback included

    def test_session_log_uses_rotating_handler(self, tmp_path):
        """Session log must use RotatingFileHandler, not plain FileHandler."""
        import logging.handlers
        slog = SessionLogger("test-sess", base_dir=tmp_path)
        assert isinstance(slog._handler, logging.handlers.RotatingFileHandler)
        slog.close()
