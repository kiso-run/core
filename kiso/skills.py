"""Skill discovery, validation, and execution."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

import tomllib

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

# Supported arg types in kiso.toml [kiso.skill.args]
_ARG_TYPES = {"string", "int", "float", "bool"}

# Module-level cache for the default skills directory.
# Only active when discover_skills() is called without a custom skills_dir.
_skills_cache: list[dict] | None = None


def invalidate_skills_cache() -> None:
    """Clear the skills cache. Call after install/remove/update."""
    global _skills_cache
    _skills_cache = None

MAX_ARGS_SIZE = 64 * 1024  # 64 KB
MAX_ARGS_DEPTH = 5


class SkillError(Exception):
    """Skill discovery, validation, or execution error."""


def _validate_manifest(manifest: dict, skill_dir: Path) -> list[str]:
    """Validate a kiso.toml manifest. Returns list of error strings."""
    errors: list[str] = []

    kiso = manifest.get("kiso")
    if not isinstance(kiso, dict):
        errors.append("missing [kiso] section")
        return errors

    if kiso.get("type") != "skill":
        errors.append(f"kiso.type must be 'skill', got {kiso.get('type')!r}")

    if not kiso.get("name") or not isinstance(kiso.get("name"), str):
        errors.append("kiso.name is required and must be a string")

    skill_section = kiso.get("skill")
    if not isinstance(skill_section, dict):
        errors.append("missing [kiso.skill] section")
        return errors

    if not skill_section.get("summary") or not isinstance(skill_section.get("summary"), str):
        errors.append("kiso.skill.summary is required and must be a string")

    # Validate args schema
    args_section = skill_section.get("args", {})
    if not isinstance(args_section, dict):
        errors.append("[kiso.skill.args] must be a table")
    else:
        for arg_name, arg_def in args_section.items():
            if not isinstance(arg_def, dict):
                errors.append(f"arg '{arg_name}' must be a table")
                continue
            arg_type = arg_def.get("type")
            if arg_type not in _ARG_TYPES:
                errors.append(
                    f"arg '{arg_name}': type must be one of {_ARG_TYPES}, got {arg_type!r}"
                )

    # Validate env declarations
    env_section = skill_section.get("env", {})
    if not isinstance(env_section, dict):
        errors.append("[kiso.skill.env] must be a table")

    # Validate session_secrets
    session_secrets = skill_section.get("session_secrets")
    if session_secrets is not None and not isinstance(session_secrets, list):
        errors.append("kiso.skill.session_secrets must be a list of strings")

    # Validate usage_guide (required string)
    if not skill_section.get("usage_guide") or not isinstance(skill_section.get("usage_guide"), str):
        errors.append("kiso.skill.usage_guide is required and must be a string")

    # Check required files
    if not (skill_dir / "run.py").exists():
        errors.append("run.py is missing")
    if not (skill_dir / "pyproject.toml").exists():
        errors.append("pyproject.toml is missing")

    return errors


def _env_var_name(skill_name: str, key: str) -> str:
    """Build env var name: KISO_SKILL_{NAME}_{KEY}."""
    name_part = skill_name.upper().replace("-", "_")
    key_part = key.upper().replace("-", "_")
    return f"KISO_SKILL_{name_part}_{key_part}"


def discover_skills(skills_dir: Path | None = None) -> list[dict]:
    """Scan ~/.kiso/skills/ and return list of valid skill info dicts.

    Each dict has: name, summary, args_schema, env, session_secrets, path,
    version, description.

    Skips directories with .installing marker.

    Results are cached at module level when using the default skills directory.
    Call invalidate_skills_cache() after install/remove/update to force re-scan.
    """
    global _skills_cache
    use_cache = skills_dir is None
    if use_cache and _skills_cache is not None:
        return _skills_cache

    skills_dir = skills_dir or (KISO_DIR / "skills")
    if not skills_dir.is_dir():
        return []

    skills: list[dict] = []
    seen_names: set[str] = set()
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue

        # Skip installing skills
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

        errors = _validate_manifest(manifest, entry)
        if errors:
            log.warning("Skill %s has manifest errors: %s", entry.name, errors)
            continue

        kiso = manifest["kiso"]
        name = kiso["name"]
        if name in seen_names:
            log.warning("Duplicate skill name '%s' in %s (skipped)", name, entry)
            continue
        seen_names.add(name)

        skill_section = kiso["skill"]
        args_schema = skill_section.get("args", {})
        env_decl = skill_section.get("env", {})
        session_secrets = skill_section.get("session_secrets", [])

        # usage_guide: local override file takes priority over toml default
        usage_guide_default = skill_section.get("usage_guide", "")
        override_path = entry / "usage_guide.local.md"
        if override_path.is_file():
            usage_guide = override_path.read_text().strip()
        else:
            usage_guide = usage_guide_default

        skills.append({
            "name": kiso["name"],
            "summary": skill_section["summary"],
            "args_schema": args_schema,
            "env": env_decl,
            "session_secrets": session_secrets or [],
            "path": str(entry),
            "version": kiso.get("version", "0.0.0"),
            "description": kiso.get("description", ""),
            "usage_guide": usage_guide,
        })

    if use_cache:
        _skills_cache = skills
    return skills


def check_deps(skill: dict) -> list[str]:
    """Check [kiso.deps].bin entries with `which`. Returns list of missing binaries."""
    skill_dir = Path(skill["path"])
    toml_path = skill_dir / "kiso.toml"

    if not toml_path.exists():
        return []

    with open(toml_path, "rb") as f:
        manifest = tomllib.load(f)

    deps = manifest.get("kiso", {}).get("deps", {})
    bins = deps.get("bin", [])
    if not isinstance(bins, list):
        return []

    missing: list[str] = []
    for b in bins:
        if not shutil.which(b):
            missing.append(b)
    return missing


def build_planner_skill_list(
    skills: list[dict],
    user_role: str = "admin",
    user_skills: str | list[str] | None = None,
) -> str:
    """Build the skill list text for the planner context.

    Filters skills based on user role and skills field:
    - admin: sees all skills
    - user with skills="*": sees all skills
    - user with skills=["a","b"]: sees only listed skills
    """
    if not skills:
        return ""

    # Filter by user access
    if user_role != "admin" and user_skills != "*":
        allowed = set(user_skills) if isinstance(user_skills, list) else set()
        skills = [s for s in skills if s["name"] in allowed]

    if not skills:
        return ""

    lines: list[str] = ["Available skills:"]
    for s in skills:
        lines.append(f"- {s['name']} — {s['summary']}")
        args_schema = s.get("args_schema", {})
        for arg_name, arg_def in args_schema.items():
            arg_type = arg_def.get("type", "string")
            required = arg_def.get("required", False)
            req_str = "required" if required else "optional"
            default = arg_def.get("default")
            desc = arg_def.get("description", "")
            parts = [f"  args: {arg_name} ({arg_type}, {req_str}"]
            if default is not None:
                parts[0] += f", default={default}"
            parts[0] += f"): {desc}"
            lines.append(parts[0])

        guide = s.get("usage_guide", "")
        if guide:
            lines.append(f"  guide: {guide}")

    return "\n".join(lines)


def _check_args_depth(obj: object, depth: int = 0) -> bool:
    """Check that args nesting depth does not exceed MAX_ARGS_DEPTH."""
    if depth > MAX_ARGS_DEPTH:
        return False
    if isinstance(obj, dict):
        return all(_check_args_depth(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return all(_check_args_depth(v, depth + 1) for v in obj)
    return True


def _coerce_value(value: object, expected_type: str) -> object:
    """Coerce a JSON value to the expected type. Returns coerced value or raises ValueError."""
    if expected_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"expected string, got {type(value).__name__}")
        return value
    elif expected_type == "int":
        if isinstance(value, bool):
            raise ValueError("expected int, got bool")
        if isinstance(value, int):
            return value
        raise ValueError(f"expected int, got {type(value).__name__}")
    elif expected_type == "float":
        if isinstance(value, bool):
            raise ValueError("expected float, got bool")
        if isinstance(value, (int, float)):
            return float(value)
        raise ValueError(f"expected float, got {type(value).__name__}")
    elif expected_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"expected bool, got {type(value).__name__}")
        return value
    raise ValueError(f"unknown type {expected_type!r}")


def validate_skill_args(args: dict, args_schema: dict) -> list[str]:
    """Validate parsed skill args against the schema. Returns list of errors."""
    errors: list[str] = []

    # Size check (on original JSON)
    args_json = json.dumps(args)
    if len(args_json) > MAX_ARGS_SIZE:
        errors.append(f"args JSON exceeds {MAX_ARGS_SIZE} bytes")
        return errors

    # Depth check
    if not _check_args_depth(args):
        errors.append(f"args nesting depth exceeds {MAX_ARGS_DEPTH}")
        return errors

    # Check required args
    for arg_name, arg_def in args_schema.items():
        required = arg_def.get("required", False)
        if required and arg_name not in args:
            errors.append(f"missing required arg: {arg_name}")

    # Type check provided args
    for arg_name, value in args.items():
        if arg_name not in args_schema:
            # Unknown arg — allow but warn
            continue
        expected_type = args_schema[arg_name].get("type", "string")
        try:
            _coerce_value(value, expected_type)
        except ValueError as e:
            errors.append(f"arg '{arg_name}': {e}")

    return errors


def build_skill_input(
    skill: dict,
    args: dict,
    session: str,
    workspace: str,
    session_secrets: dict[str, str] | None = None,
    plan_outputs: list[dict] | None = None,
) -> dict:
    """Build the input JSON dict for a skill subprocess."""
    # Scope session_secrets to only declared ones
    declared = set(skill.get("session_secrets", []))
    scoped_secrets: dict[str, str] = {}
    if session_secrets:
        scoped_secrets = {k: v for k, v in session_secrets.items() if k in declared}

    return {
        "args": args,
        "session": session,
        "workspace": workspace,
        "session_secrets": scoped_secrets,
        "plan_outputs": plan_outputs or [],
    }


def build_skill_env(skill: dict) -> dict[str, str]:
    """Build the environment dict for a skill subprocess.

    Includes PATH + deploy secret env vars (KISO_SKILL_{NAME}_{KEY}).
    """
    env: dict[str, str] = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    env_decl = skill.get("env", {})
    skill_name = skill["name"]
    for key, decl in env_decl.items():
        var_name = _env_var_name(skill_name, key)
        value = os.environ.get(var_name)
        if value is not None:
            env[var_name] = value
        elif isinstance(decl, dict) and decl.get("required"):
            log.warning("Env var %s not set (required by skill %s)", var_name, skill_name)

    return env
