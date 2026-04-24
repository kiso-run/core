"""Startup-time config consistency checks.

Separate module so the validators can be called both by the daemon
boot path (with the live MCP / skill catalogs) and by unit tests
(with fixture catalogs). No side effects except the emit helpers,
which log to the module logger.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Iterable

from kiso.config import Config

log = logging.getLogger(__name__)


__all__ = (
    "validate_user_allowlists",
    "emit_allowlist_warnings",
)


def _entries(value) -> list[str]:
    if value is None or value == "*":
        return []  # wildcard / default — nothing to validate
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


def _entry_matches(entry: str, catalog: Iterable[str]) -> bool:
    """Return True if ``entry`` matches at least one catalog item.

    Plain strings match exactly. Glob-style wildcards (``*``, ``?``,
    ``[seq]``) are honoured via ``fnmatch.fnmatchcase``.
    """
    if any(ch in entry for ch in ("*", "?", "[")):
        return any(fnmatch.fnmatchcase(item, entry) for item in catalog)
    return entry in catalog


def validate_user_allowlists(
    config: Config,
    *,
    mcp_methods: list[str],
    skill_names: list[str],
) -> list[tuple[str, str, str]]:
    """Return per-user allowlist entries that do not match the catalogs.

    Each tuple is ``(user, key, unknown_entry)`` where ``key`` is
    either ``"mcp"`` or ``"skills"``. Wildcards (``*``) and ``None``
    are never reported — only explicit entries that fail to match
    anything.
    """
    mcp_catalog = list(mcp_methods)
    skill_catalog = list(skill_names)
    out: list[tuple[str, str, str]] = []
    for uname, user in config.users.items():
        for entry in _entries(user.mcp):
            if not _entry_matches(entry, mcp_catalog):
                out.append((uname, "mcp", entry))
        for entry in _entries(user.skills):
            if not _entry_matches(entry, skill_catalog):
                out.append((uname, "skills", entry))
    return out


def emit_allowlist_warnings(
    issues: list[tuple[str, str, str]],
) -> None:
    """Log one WARNING per offending entry with the user / key / entry."""
    for user, key, unknown in issues:
        log.warning(
            "[users.%s.%s].allow entry %r matches no installed %s",
            user, key, unknown,
            "MCP method" if key == "mcp" else "skill",
        )
