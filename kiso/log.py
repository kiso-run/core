"""Centralized logging — server log + per-session logs."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from kiso.config import KISO_DIR

_LOG_FMT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_SERVER_LOG = "server.log"
_SERVER_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_SERVER_BACKUP_COUNT = 3


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the ``kiso`` root logger.

    Handlers:
    - ``~/.kiso/server.log`` — RotatingFileHandler (5 MB, 3 backups)
    - stderr — StreamHandler (container / dev visibility)

    Idempotent: skips if handlers are already attached.
    """
    root = logging.getLogger("kiso")
    if root.handlers:
        return

    root.setLevel(level)
    fmt = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)

    # File handler — server.log
    log_path = KISO_DIR / _SERVER_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=_SERVER_MAX_BYTES, backupCount=_SERVER_BACKUP_COUNT,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Stderr handler
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


class SessionLogger:
    """Per-session file logger writing to ``sessions/{session}/session.log``.

    Call :meth:`close` when the worker shuts down.
    """

    def __init__(self, session: str, base_dir: Path | None = None):
        self.session = session
        base = base_dir or KISO_DIR
        self._name = f"kiso.session.{session}"
        self._logger = logging.getLogger(self._name)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False  # don't duplicate into server.log

        fmt = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)
        log_dir = base / "sessions" / session
        log_dir.mkdir(parents=True, exist_ok=True)
        self._handler = logging.FileHandler(log_dir / "session.log")
        self._handler.setFormatter(fmt)
        self._logger.addHandler(self._handler)

    def info(self, msg: str, *args: object) -> None:
        self._logger.info(msg, *args)

    def warning(self, msg: str, *args: object) -> None:
        self._logger.warning(msg, *args)

    def error(self, msg: str, *args: object) -> None:
        self._logger.error(msg, *args)

    def close(self) -> None:
        """Remove handler and close the log file."""
        self._logger.removeHandler(self._handler)
        self._handler.close()
