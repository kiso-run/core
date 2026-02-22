"""Public file URL helpers."""

import hashlib
import hmac

from kiso.config import KISO_DIR, Config


def pub_token(session: str, config: Config) -> str:
    """Compute HMAC token for a session's pub/ directory."""
    key = (config.tokens.get("cli") or "kiso").encode()
    return hmac.new(key, session.encode(), hashlib.sha256).hexdigest()[:16]


def resolve_pub_token(token: str, config: Config) -> str | None:
    """Find which session matches a pub token."""
    sessions_dir = KISO_DIR / "sessions"
    if not sessions_dir.is_dir():
        return None
    for entry in sessions_dir.iterdir():
        if entry.is_dir() and pub_token(entry.name, config) == token:
            return entry.name
    return None
