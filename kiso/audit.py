"""Structured audit logging — JSONL files in ~/.kiso/audit/."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.security import sanitize_output

log = logging.getLogger(__name__)

# Fields exempt from secret masking (structural, never user-supplied).
_MASK_EXEMPT = frozenset({"timestamp", "type"})

# Tracks audit directories already initialised in this process.
# Avoids mkdir + chmod on every write once the directory exists.
_audit_dir_ready: set[Path] = set()


def _ensure_audit_dir(audit_dir: Path) -> None:
    """Create audit dir and set permissions, at most once per process per path."""
    if audit_dir in _audit_dir_ready:
        return
    audit_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(audit_dir, 0o700)
    _audit_dir_ready.add(audit_dir)


def _write_entry(
    entry: dict,
    deploy_secrets: dict[str, str] | None = None,
    session_secrets: dict[str, str] | None = None,
) -> None:
    """Write a single audit entry as a JSONL line.

    - Adds ISO 8601 timestamp
    - Sanitizes string fields when secrets are provided
    - Creates audit directory if needed
    - Never raises — audit failures are logged and swallowed
    """
    try:
        now = datetime.now(timezone.utc)
        entry = {**entry, "timestamp": now.isoformat()}

        if deploy_secrets or session_secrets:
            ds = deploy_secrets or {}
            ss = session_secrets or {}
            for key, value in entry.items():
                if isinstance(value, str) and key not in _MASK_EXEMPT:
                    entry[key] = sanitize_output(value, ds, ss)

        audit_dir = KISO_DIR / "audit"
        _ensure_audit_dir(audit_dir)

        today = now.strftime("%Y-%m-%d")
        path = audit_dir / f"{today}.jsonl"
        opener = lambda p, flags: os.open(p, flags, 0o600)
        with open(path, "a", opener=opener) as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        log.warning("Audit write failed", exc_info=True)


def log_llm_call(
    session: str,
    role: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    status: str,
) -> None:
    """Log an LLM call."""
    _write_entry({
        "type": "llm",
        "session": session,
        "role": role,
        "model": model,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "status": status,
    })


def log_task(
    session: str,
    task_id: int,
    task_type: str,
    detail: str,
    status: str,
    duration_ms: int,
    output_length: int,
    deploy_secrets: dict[str, str] | None = None,
    session_secrets: dict[str, str] | None = None,
) -> None:
    """Log a task execution. Detail is sanitized against known secrets."""
    _write_entry(
        {
            "type": "task",
            "session": session,
            "task_id": task_id,
            "task_type": task_type,
            "detail": detail,
            "status": status,
            "duration_ms": duration_ms,
            "output_length": output_length,
        },
        deploy_secrets=deploy_secrets,
        session_secrets=session_secrets,
    )


def log_review(
    session: str,
    task_id: int,
    verdict: str,
    has_learning: bool,
) -> None:
    """Log a review verdict."""
    _write_entry({
        "type": "review",
        "session": session,
        "task_id": task_id,
        "verdict": verdict,
        "has_learning": has_learning,
    })


def log_webhook(
    session: str,
    task_id: int,
    url: str,
    status: int,
    attempts: int,
    deploy_secrets: dict[str, str] | None = None,
    session_secrets: dict[str, str] | None = None,
) -> None:
    """Log a webhook delivery. URL is sanitized against known secrets."""
    _write_entry(
        {
            "type": "webhook",
            "session": session,
            "task_id": task_id,
            "url": url,
            "status": status,
            "attempts": attempts,
        },
        deploy_secrets=deploy_secrets,
        session_secrets=session_secrets,
    )
