"""Connector discovery and manifest validation (server-side)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.plugins import _scan_plugin_dirs, _validate_plugin_manifest_base, plugin_env_var_name

log = logging.getLogger(__name__)

CONNECTORS_DIR = KISO_DIR / "connectors"

# TTL cache for discover_connectors() — mirrors tools.py pattern.
_CONNECTORS_TTL: float = 30.0
_connectors_cache: dict[Path, tuple[float, list[dict]]] = {}


def invalidate_connectors_cache() -> None:
    """Clear the discover_connectors() TTL cache.

    Call after installing or removing a connector so the next
    discover_connectors() call rescans the directory.
    """
    _connectors_cache.clear()


def _validate_connector_manifest(manifest: dict, connector_dir: Path) -> list[str]:
    """Validate a kiso.toml manifest for connectors. Returns list of error strings."""
    return _validate_plugin_manifest_base(manifest, connector_dir, "connector")


def _connector_env_var_name(connector_name: str, key: str) -> str:
    """Build env var name: KISO_CONNECTOR_{NAME}_{KEY}."""
    return plugin_env_var_name("CONNECTOR", connector_name, key)


def discover_connectors(connectors_dir: Path | None = None) -> list[dict]:
    """Scan ~/.kiso/connectors/ and return list of valid connector info dicts.

    Each dict has: name, version, description, platform, path.

    Skips directories with .installing marker.

    Results are cached per directory for _CONNECTORS_TTL seconds.
    Call invalidate_connectors_cache() after install/remove.
    """
    resolved_dir = connectors_dir or CONNECTORS_DIR

    now = time.monotonic()
    cached = _connectors_cache.get(resolved_dir)
    if cached is not None and now - cached[0] < _CONNECTORS_TTL:
        return cached[1]

    if not resolved_dir.is_dir():
        return []

    connectors: list[dict] = []
    for entry, manifest in _scan_plugin_dirs(resolved_dir, _validate_connector_manifest):
        kiso = manifest["kiso"]
        connector_section = kiso.get("connector", {})

        connectors.append({
            "name": kiso["name"],
            "version": kiso.get("version", "0.0.0"),
            "description": kiso.get("description", ""),
            "platform": connector_section.get("platform", ""),
            "path": str(entry),
        })

    _connectors_cache[resolved_dir] = (now, connectors)
    return connectors
