"""M690: Preset manifest format, validation, and loading."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PresetManifest:
    """Structured representation of a preset.toml manifest."""

    name: str
    version: str
    description: str
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)
    knowledge_facts: list[dict] = field(default_factory=list)
    behaviors: list[str] = field(default_factory=list)
    env_vars: dict[str, dict] = field(default_factory=dict)


_VALID_FACT_KEYS = {"content", "category", "tags"}
_VALID_FACT_CATEGORIES = frozenset(
    {"general", "project", "tool", "user", "system", "behavior"}
)


def validate_preset_manifest(manifest: dict) -> list[str]:
    """Validate a raw parsed TOML manifest dict. Returns list of errors (empty = valid)."""
    errors: list[str] = []

    kiso = manifest.get("kiso")
    if not isinstance(kiso, dict):
        errors.append("Missing [kiso] section")
        return errors

    if kiso.get("type") != "preset":
        errors.append("kiso.type must be 'preset'")

    for req in ("name", "version", "description"):
        val = kiso.get(req)
        if not val or not isinstance(val, str) or not val.strip():
            errors.append(f"kiso.{req} is required and must be a non-empty string")

    preset = kiso.get("preset")
    if not isinstance(preset, dict):
        errors.append("Missing [kiso.preset] section")
        return errors

    # Validate list-of-string fields
    for list_field in ("tools", "skills", "connectors"):
        val = preset.get(list_field, [])
        if not isinstance(val, list):
            errors.append(f"kiso.preset.{list_field} must be a list")
        elif not all(isinstance(item, str) for item in val):
            errors.append(f"kiso.preset.{list_field} must contain only strings")

    # Validate knowledge section
    knowledge = preset.get("knowledge", {})
    if not isinstance(knowledge, dict):
        errors.append("kiso.preset.knowledge must be a table")
    else:
        facts = knowledge.get("facts", [])
        if not isinstance(facts, list):
            errors.append("kiso.preset.knowledge.facts must be a list")
        else:
            for i, fact in enumerate(facts):
                if not isinstance(fact, dict):
                    errors.append(f"knowledge.facts[{i}]: must be a table")
                    continue
                if "content" not in fact or not isinstance(fact["content"], str) or not fact["content"].strip():
                    errors.append(f"knowledge.facts[{i}]: content is required")
                cat = fact.get("category", "general")
                if cat not in _VALID_FACT_CATEGORIES:
                    errors.append(
                        f"knowledge.facts[{i}]: invalid category '{cat}'"
                    )
                tags = fact.get("tags", [])
                if not isinstance(tags, list) or not all(
                    isinstance(t, str) for t in tags
                ):
                    errors.append(f"knowledge.facts[{i}]: tags must be a list of strings")

        behaviors = knowledge.get("behaviors", [])
        if not isinstance(behaviors, list):
            errors.append("kiso.preset.knowledge.behaviors must be a list")
        elif not all(isinstance(b, str) and b.strip() for b in behaviors):
            errors.append(
                "kiso.preset.knowledge.behaviors must contain non-empty strings"
            )

    # Validate env section
    env = preset.get("env", {})
    if not isinstance(env, dict):
        errors.append("kiso.preset.env must be a table")
    else:
        for key, val in env.items():
            if not isinstance(val, dict):
                errors.append(f"kiso.preset.env.{key} must be a table")

    return errors


def _manifest_from_dict(kiso: dict) -> PresetManifest:
    """Build a PresetManifest from the validated [kiso] section."""
    preset = kiso.get("preset", {})
    knowledge = preset.get("knowledge", {})
    return PresetManifest(
        name=kiso["name"],
        version=kiso["version"],
        description=kiso["description"],
        tools=preset.get("tools", []),
        skills=preset.get("skills", []),
        connectors=preset.get("connectors", []),
        knowledge_facts=knowledge.get("facts", []),
        behaviors=knowledge.get("behaviors", []),
        env_vars=preset.get("env", {}),
    )


def load_preset(path: Path) -> PresetManifest:
    """Load and validate a preset.toml file. Raises ValueError on errors."""
    if not path.is_file():
        raise FileNotFoundError(f"Preset file not found: {path}")

    with open(path, "rb") as f:
        manifest = tomllib.load(f)

    errors = validate_preset_manifest(manifest)
    if errors:
        raise ValueError(
            f"Invalid preset manifest ({path}):\n" + "\n".join(f"  - {e}" for e in errors)
        )

    return _manifest_from_dict(manifest["kiso"])
