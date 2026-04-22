"""User-managed install-time trust store at ``~/.kiso/trust.json``.

Keeps per-type prefix lists for ``mcp`` and ``skill``. Tier-1
prefixes are hardcoded in ``kiso/mcp/trust.py`` and
``kiso/skill_trust.py``; this module carries only the
user-editable extension list.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from kiso import config as _config


TRUST_PATH: Path = _config.KISO_DIR / "trust.json"

_SUPPORTED_TYPES = ("mcp", "skill")


@dataclass
class TrustStore:
    mcp: list[str] = field(default_factory=list)
    skill: list[str] = field(default_factory=list)


def load_trust_store() -> TrustStore:
    """Return the persisted trust store, or an empty one when missing."""
    path = _current_path()
    if not path.exists():
        return TrustStore()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TrustStore()
    if not isinstance(raw, dict):
        return TrustStore()
    return TrustStore(
        mcp=_coerce_str_list(raw.get("mcp")),
        skill=_coerce_str_list(raw.get("skill")),
    )


def save_trust_store(store: TrustStore) -> None:
    """Atomically write the store to ``TRUST_PATH``."""
    path = _current_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"mcp": list(store.mcp), "skill": list(store.skill)},
        indent=2,
        sort_keys=True,
    )
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_prefix(scope: str, prefix: str) -> None:
    """Append *prefix* to the *scope* list (no-op if already present)."""
    _assert_scope(scope)
    store = load_trust_store()
    current = getattr(store, scope)
    if prefix not in current:
        current.append(prefix)
        save_trust_store(store)


def remove_prefix(scope: str, prefix: str) -> None:
    """Drop *prefix* from the *scope* list (no-op if absent)."""
    _assert_scope(scope)
    store = load_trust_store()
    current = getattr(store, scope)
    if prefix in current:
        current.remove(prefix)
        save_trust_store(store)


def matches_any_prefix(source: str, prefixes: list[str] | tuple[str, ...]) -> bool:
    """True iff *source* is covered by any prefix in *prefixes*.

    - ``prefix*`` — glob. Matches when *source* starts with the head.
    - bare or ``prefix/`` — path-prefix. Matches when *source* equals
      the prefix or starts with ``<prefix>/`` (trailing ``/`` is
      treated as equivalent to its absence so both forms work).

    Case-sensitive — GitHub preserves case on owner/repo.
    """
    for p in prefixes:
        if p.endswith("*"):
            if source.startswith(p[:-1]):
                return True
            continue
        head = p.rstrip("/")
        if source == head or source.startswith(head + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_path() -> Path:
    # Re-read the module attribute every call so monkeypatching
    # ``TRUST_PATH`` in tests takes effect immediately.
    import kiso.trust_store as _self
    return _self.TRUST_PATH


def _assert_scope(scope: str) -> None:
    if scope not in _SUPPORTED_TYPES:
        raise ValueError(
            f"unknown trust scope {scope!r} "
            f"(expected one of {_SUPPORTED_TYPES})"
        )


def _coerce_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str)]
