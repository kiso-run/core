"""Official plugin registry — fetch and search available tools/connectors."""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger(__name__)

REGISTRY_URL = (
    "https://raw.githubusercontent.com/kiso-run/core/main/registry.json"
)

# In-memory TTL cache so we don't hit GitHub on every planner call.
_registry_cache: dict | None = None
_registry_ts: float = 0.0
_REGISTRY_TTL: float = 300.0  # 5 minutes


def fetch_registry() -> dict:
    """Fetch the official registry from GitHub, cached for 5 min.

    Returns ``{}`` on network/parse errors — callers must tolerate empty.
    """
    import httpx

    global _registry_cache, _registry_ts  # noqa: PLW0603
    now = time.monotonic()
    if _registry_cache is not None and (now - _registry_ts) < _REGISTRY_TTL:
        return _registry_cache
    try:
        resp = httpx.get(REGISTRY_URL, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        _registry_cache = json.loads(resp.text)
        _registry_ts = now
        return _registry_cache
    except Exception as exc:
        log.warning("Failed to fetch registry: %s", exc)
        return _registry_cache or {}


def search_entries(entries: list[dict], query: str | None) -> list[dict]:
    """Filter registry entries: match name first, then description."""
    if not query:
        return entries
    q = query.lower()
    by_name = [e for e in entries if q in e["name"].lower()]
    if by_name:
        return by_name
    return [e for e in entries if q in e.get("description", "").lower()]


def cross_type_hint(registry: dict, current_type: str, query: str) -> str | None:
    """Check the other plugin type for matches and return a hint string.

    When a search in one type (e.g. "connectors") yields no results, this
    function checks the other type ("tools") for matches.  Returns a hint
    string like 'Did you mean `kiso tool search browser`?' or ``None``.
    """
    if current_type == "connectors":
        other_type = "tools"
        other_cmd = "tool"
    else:
        other_type = "connectors"
        other_cmd = "connector"
    # Backward compat: old registries use "skills" key instead of "tools"
    other_entries = registry.get(
        other_type,
        registry.get("skills", []) if other_type == "tools" else [],
    )
    matches = search_entries(other_entries, query)
    if matches:
        names = ", ".join(m["name"] for m in matches)
        return (
            f"Did you mean `kiso {other_cmd} search {query}`? "
            f"Found in {other_type}: {names}"
        )
    return None


def get_registry_tools(installed_names: set[str]) -> str:
    """Return formatted list of available-but-not-installed registry tools.

    Returns empty string when registry is unavailable or all tools are
    already installed.
    """
    reg = fetch_registry()
    tools = reg.get("tools", [])
    uninstalled = [t for t in tools if t["name"] not in installed_names]
    if not uninstalled:
        return ""
    lines = [f"- {t['name']} — {t['description']}" for t in uninstalled]
    return "Available tools (not installed):\n" + "\n".join(lines)
