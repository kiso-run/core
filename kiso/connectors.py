"""Connector discovery and manifest validation (server-side)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.plugins import _validate_plugin_manifest_base

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
    name_part = connector_name.upper().replace("-", "_")
    key_part = key.upper().replace("-", "_")
    return f"KISO_CONNECTOR_{name_part}_{key_part}"


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

    import tomllib

    connectors: list[dict] = []
    for entry in sorted(resolved_dir.iterdir()):
        if not entry.is_dir():
            continue

        if (entry / ".installing").exists():
            continue

        toml_path = entry / "kiso.toml"
        if not toml_path.exists():
            continue

        try:
            with open(toml_path, "rb") as f:
                manifest = tomllib.load(f)
        except Exception as e:
            log.warning("Skipping connector %s: failed to read kiso.toml: %s", entry.name, e)
            continue

        errors = _validate_connector_manifest(manifest, entry)
        if errors:
            log.warning("Skipping connector %s: invalid manifest: %s", entry.name, "; ".join(errors))
            continue

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
