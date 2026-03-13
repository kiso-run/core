"""Shared plugin validation utilities (tool, connector)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import tomllib

log = logging.getLogger(__name__)


def _validate_plugin_manifest_base(
    manifest: dict, plugin_dir: Path, plugin_type: str,
) -> list[str]:
    """Validate fields common to all kiso plugins (tool, connector).

    Returns list of error strings.  Early-returns on structural errors
    ([kiso] or [kiso.{plugin_type}] missing) so callers can detect when
    further field-level validation is not possible.
    """
    errors: list[str] = []

    kiso = manifest.get("kiso")
    if not isinstance(kiso, dict):
        errors.append("missing [kiso] section")
        return errors

    if kiso.get("type") != plugin_type:
        errors.append(f"kiso.type must be '{plugin_type}', got {kiso.get('type')!r}")

    if not kiso.get("name") or not isinstance(kiso.get("name"), str):
        errors.append("kiso.name is required and must be a string")

    section = kiso.get(plugin_type)
    if not isinstance(section, dict):
        errors.append(f"missing [kiso.{plugin_type}] section")
        return errors

    if not (plugin_dir / "run.py").exists():
        errors.append("run.py is missing")
    if not (plugin_dir / "pyproject.toml").exists():
        errors.append("pyproject.toml is missing")

    return errors


def plugin_env_var_name(plugin_type: str, plugin_name: str, key: str) -> str:
    """Build env var name: KISO_{TYPE}_{NAME}_{KEY}."""
    type_part = plugin_type.upper()
    name_part = plugin_name.upper().replace("-", "_")
    key_part = key.upper().replace("-", "_")
    return f"KISO_{type_part}_{name_part}_{key_part}"


def _scan_plugin_dirs(
    parent_dir: Path,
    validate_fn: Callable[[dict, Path], list[str]],
) -> list[tuple[Path, dict]]:
    """Scan a plugin directory, load and validate manifests.

    Returns list of (plugin_dir, manifest) tuples for valid plugins.
    Skips dirs with .installing marker or invalid manifests.
    """
    if not parent_dir.is_dir():
        return []

    results: list[tuple[Path, dict]] = []
    for entry in sorted(parent_dir.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / ".installing").exists():
            log.debug("Skipping %s (installing)", entry.name)
            continue
        toml_path = entry / "kiso.toml"
        if not toml_path.exists():
            log.debug("Skipping %s (no kiso.toml)", entry.name)
            continue
        try:
            with open(toml_path, "rb") as f:
                manifest = tomllib.load(f)
        except Exception as e:
            log.warning("Failed to parse %s: %s", toml_path, e)
            continue
        errors = validate_fn(manifest, entry)
        if errors:
            log.warning("Plugin %s has manifest errors: %s", entry.name, errors)
            continue
        results.append((entry, manifest))
    return results
