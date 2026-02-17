"""Structured audit logging — JSONL files in ~/.kiso/audit/."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.security import sanitize_output

log = logging.getLogger(__name__)


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
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()

        if deploy_secrets or session_secrets:
            ds = deploy_secrets or {}
            ss = session_secrets or {}
            for key, value in entry.items():
                if isinstance(value, str) and key != "timestamp":
                    entry[key] = sanitize_output(value, ds, ss)

        audit_dir = KISO_DIR / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = audit_dir / f"{today}.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
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
) -> None:
    """Log a webhook delivery."""
    _write_entry({
        "type": "webhook",
        "session": session,
        "task_id": task_id,
        "url": url,
        "status": status,
        "attempts": attempts,
    })
