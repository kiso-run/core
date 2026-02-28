"""Shared plugin validation utilities (skill and connector)."""

from __future__ import annotations

from pathlib import Path


def _validate_plugin_manifest_base(
    manifest: dict, plugin_dir: Path, plugin_type: str,
) -> list[str]:
    """Validate fields common to all kiso plugins (skill, connector).

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
