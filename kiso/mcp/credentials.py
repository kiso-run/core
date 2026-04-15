"""Per-server credential persistence for MCP runtime auth.

OAuth tokens (and any other secret kiso receives at runtime via
an interactive flow) are persisted under
``~/.kiso/mcp/credentials/<server>.json`` with mode ``0600`` and
the parent directory at mode ``0700``. The format is plain JSON
so a future tool or a curious user can inspect it without
reaching for a special decoder.

The store is intentionally unaware of refresh-token semantics:
when an access token expires, the caller re-runs the original
flow (e.g. device flow) and overwrites the file. This keeps the
v0.9 surface tiny — refresh handling lands in a follow-up when
real provider integrations need it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from kiso.config import KISO_DIR


__all__ = [
    "CredentialsError",
    "delete_credential",
    "load_credential",
    "save_credential",
    "server_credentials_path",
]


_CREDENTIALS_SUBDIR = ("mcp", "credentials")
_DIR_MODE = 0o700
_FILE_MODE = 0o600


class CredentialsError(Exception):
    """Raised on path validation, JSON encode/decode, or filesystem errors."""


def _credentials_dir() -> Path:
    return KISO_DIR / _CREDENTIALS_SUBDIR[0] / _CREDENTIALS_SUBDIR[1]


def _validate_server_name(name: str) -> None:
    if not name:
        raise CredentialsError("server name must be non-empty")
    if "/" in name or "\\" in name or ".." in name:
        raise CredentialsError(
            f"server name {name!r} contains path-traversal characters; "
            f"must be a plain identifier"
        )


def server_credentials_path(name: str) -> Path:
    """Return the absolute path where *name* credentials would be stored.

    Validates *name* against path-traversal characters. Does not
    require the file (or its parent directory) to exist.
    """
    _validate_server_name(name)
    return _credentials_dir() / f"{name}.json"


def _ensure_credentials_dir() -> Path:
    creds_dir = _credentials_dir()
    creds_dir.mkdir(parents=True, exist_ok=True)
    # Tighten perms even if the dir already existed with looser perms.
    try:
        os.chmod(creds_dir, _DIR_MODE)
    except OSError as exc:
        raise CredentialsError(
            f"failed to set {creds_dir} mode to {oct(_DIR_MODE)}: {exc}"
        ) from exc
    return creds_dir


def save_credential(name: str, payload: dict) -> Path:
    """Persist *payload* as the credential blob for server *name*.

    Adds a ``saved_at`` Unix timestamp to the payload before
    writing. The file is written with mode ``0600`` and the
    parent directory is created with mode ``0700`` if missing.
    Returns the path written.

    Raises :class:`CredentialsError` if *payload* is not JSON-
    serialisable, if the path resolution fails, or if the
    filesystem write fails.
    """
    _validate_server_name(name)
    _ensure_credentials_dir()
    path = server_credentials_path(name)

    blob = dict(payload)
    blob.setdefault("saved_at", time.time())

    try:
        text = json.dumps(blob, indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise CredentialsError(
            f"cannot serialise credential for {name!r} as JSON: {exc}"
        ) from exc

    try:
        path.write_text(text)
        os.chmod(path, _FILE_MODE)
    except OSError as exc:
        raise CredentialsError(
            f"failed to write credential file {path}: {exc}"
        ) from exc
    return path


def load_credential(name: str) -> dict | None:
    """Return the persisted credential dict for server *name*, or None.

    Returns ``None`` when no credential has been saved (the file is
    missing). Raises :class:`CredentialsError` on path validation
    or JSON decode failure — corruption is a real error worth
    surfacing.
    """
    path = server_credentials_path(name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsError(
            f"failed to read credential file {path}: {exc}"
        ) from exc


def delete_credential(name: str) -> None:
    """Remove the credential file for server *name* if it exists.

    No-op when the file is missing — callers can use this to
    reset auth state without checking first.
    """
    path = server_credentials_path(name)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise CredentialsError(
            f"failed to delete credential file {path}: {exc}"
        ) from exc
