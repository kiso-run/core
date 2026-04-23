"""Registry client for the preset catalog.

Extracted from the retired ``cli/plugin_ops.py``. Fetches the official
``registry.json`` from GitHub (with a 5-minute TTL cache) and exposes
small utilities (``search_entries``, ``render_aligned_list``) that
``cli/preset.py`` uses to render ``kiso preset list`` /
``kiso preset search`` output.

Connectors, MCP servers and skills no longer go through this registry —
they are installed via config.toml / ``kiso mcp install`` / ``kiso skill
install``. Only presets still live here.
"""

from __future__ import annotations

import json
import sys
import time

REGISTRY_URL = "https://raw.githubusercontent.com/kiso-run/core/main/registry.json"

_registry_cache: dict | None = None
_registry_ts: float = 0.0
_REGISTRY_TTL: float = 300.0  # seconds


def _fetch_registry_core() -> dict:
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
        import logging

        logging.getLogger(__name__).warning("Failed to fetch registry: %s", exc)
        return _registry_cache or {}


def fetch_registry() -> dict:
    """Fetch the official registry — exits on failure (CLI use)."""
    reg = _fetch_registry_core()
    if not reg:
        print("error: failed to fetch registry")
        sys.exit(1)
    return reg


def search_entries(entries: list[dict], query: str | None) -> list[dict]:
    """Filter registry entries: match name first, then description."""
    if not query:
        return entries
    q = query.lower()
    by_name = [e for e in entries if q in e["name"].lower()]
    if by_name:
        return by_name
    return [e for e in entries if q in e.get("description", "").lower()]


def render_aligned_list(
    items: list[dict],
    name_key: str,
    desc_key: str | None = None,
    desc_fallback: str | None = None,
    extra_cols: list[str] | None = None,
) -> None:
    """Print items as aligned columns: name [extra_cols...] [— description]."""
    if not items:
        return
    max_name = max(len(str(i[name_key])) for i in items)
    max_extras: dict[str, int] = {}
    for col in extra_cols or []:
        max_extras[col] = max(len(str(i.get(col, ""))) for i in items)
    for item in items:
        parts = [f"  {str(item[name_key]).ljust(max_name)}"]
        for col in extra_cols or []:
            parts.append(str(item.get(col, "")).ljust(max_extras[col]))
        if desc_key:
            desc = item.get(desc_key, "")
            if not desc and desc_fallback:
                desc = item.get(desc_fallback, "")
            parts.append(f"— {desc}")
        print("  ".join(parts))
