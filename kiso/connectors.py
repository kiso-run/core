"""Connector discovery and manifest validation (server-side)."""

from __future__ import annotations

import logging
from pathlib import Path

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

CONNECTORS_DIR = KISO_DIR / "connectors"


def _validate_connector_manifest(manifest: dict, connector_dir: Path) -> list[str]:
    """Validate a kiso.toml manifest for connectors. Returns list of error strings."""
    errors: list[str] = []

    kiso = manifest.get("kiso")
    if not isinstance(kiso, dict):
        errors.append("missing [kiso] section")
        return errors

    if kiso.get("type") != "connector":
        errors.append(f"kiso.type must be 'connector', got {kiso.get('type')!r}")

    if not kiso.get("name") or not isinstance(kiso.get("name"), str):
        errors.append("kiso.name is required and must be a string")

    connector_section = kiso.get("connector")
    if not isinstance(connector_section, dict):
        errors.append("missing [kiso.connector] section")
        return errors

    if not (connector_dir / "run.py").exists():
        errors.append("run.py is missing")
    if not (connector_dir / "pyproject.toml").exists():
        errors.append("pyproject.toml is missing")

    return errors


def _connector_env_var_name(connector_name: str, key: str) -> str:
    """Build env var name: KISO_CONNECTOR_{NAME}_{KEY}."""
    name_part = connector_name.upper().replace("-", "_")
    key_part = key.upper().replace("-", "_")
    return f"KISO_CONNECTOR_{name_part}_{key_part}"


def discover_connectors(connectors_dir: Path | None = None) -> list[dict]:
    """Scan ~/.kiso/connectors/ and return list of valid connector info dicts.

    Each dict has: name, version, description, platform, path.

    Skips directories with .installing marker.
    """
    connectors_dir = connectors_dir or CONNECTORS_DIR
    if not connectors_dir.is_dir():
        return []

    import tomllib

    connectors: list[dict] = []
    for entry in sorted(connectors_dir.iterdir()):
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

    return connectors
