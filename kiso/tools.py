"""Tool discovery, validation, and execution."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from kiso.config import KISO_DIR, LLM_API_KEY_ENV
from kiso.plugins import _scan_plugin_dirs, _validate_plugin_manifest_base, plugin_env_var_name

log = logging.getLogger(__name__)

# Supported arg types in kiso.toml [kiso.tool.args]
_ARG_TYPES = {"string", "int", "float", "bool"}

# Valid vocabulary for kiso.tool.consumes
_CONSUMES_VOCAB = frozenset({"image", "document", "audio", "video", "code", "web_page"})

# TTL cache for discover_tools() — keyed by resolved tools dir path.
# Avoids repeated filesystem scans on every planner/executor call.
# Cleared by invalidate_tools_cache() after install/remove.
_TOOLS_TTL: float = 30.0
_tools_cache: dict[Path, tuple[float, list[dict]]] = {}


def invalidate_tools_cache() -> None:
    """Clear the discover_tools() TTL cache.

    Call after installing or removing a tool so the next
    discover_tools() call rescans the directory.
    """
    _tools_cache.clear()


MAX_ARGS_SIZE = 64 * 1024  # 64 KB
MAX_ARGS_DEPTH = 5


class ToolError(Exception):
    """Tool discovery, validation, or execution error."""


def _validate_manifest(manifest: dict, tool_dir: Path) -> list[str]:
    """Validate a kiso.toml manifest. Returns list of error strings."""
    errors = _validate_plugin_manifest_base(manifest, tool_dir, "tool")

    # Base already checked [kiso] and [kiso.tool] sections; if either is
    # missing it returned early, so re-check before accessing fields.
    kiso = manifest.get("kiso")
    if not isinstance(kiso, dict):
        return errors
    tool_section = kiso.get("tool")
    if not isinstance(tool_section, dict):
        return errors

    if not tool_section.get("summary") or not isinstance(tool_section.get("summary"), str):
        errors.append("kiso.tool.summary is required and must be a string")

    # Validate args schema
    args_section = tool_section.get("args", {})
    if not isinstance(args_section, dict):
        errors.append("[kiso.tool.args] must be a table")
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
    env_section = tool_section.get("env", {})
    if not isinstance(env_section, dict):
        errors.append("[kiso.tool.env] must be a table")

    # Validate session_secrets
    session_secrets = tool_section.get("session_secrets")
    if session_secrets is not None and not isinstance(session_secrets, list):
        errors.append("kiso.tool.session_secrets must be a list of strings")

    # Validate usage_guide (required string)
    if not tool_section.get("usage_guide") or not isinstance(tool_section.get("usage_guide"), str):
        errors.append("kiso.tool.usage_guide is required and must be a string")

    return errors


def _env_var_name(tool_name: str, key: str) -> str:
    """Build env var name: KISO_TOOL_{NAME}_{KEY}."""
    return plugin_env_var_name("TOOL", tool_name, key)


def discover_tools(tools_dir: Path | None = None) -> list[dict]:
    """Scan ~/.kiso/tools/ and return list of valid tool info dicts.

    Each dict has: name, summary, args_schema, env, session_secrets, path,
    version, description.

    Skips directories with .installing marker.

    Results are cached per directory for _TOOLS_TTL seconds to avoid
    repeated filesystem scans on every planner call. Call
    invalidate_tools_cache() after installing or removing a tool.

    """
    resolved_dir = tools_dir or (KISO_DIR / "tools")

    now = time.monotonic()
    cached = _tools_cache.get(resolved_dir)
    if cached is not None and now - cached[0] < _TOOLS_TTL:
        return cached[1]

    if not resolved_dir.is_dir():
        log.warning(
            "Tools directory not found: %s (exists=%s)",
            resolved_dir, resolved_dir.exists(),
        )
        return []

    tools: list[dict] = []
    seen_names: set[str] = set()
    for entry, manifest in _scan_plugin_dirs(resolved_dir, _validate_manifest):
        kiso = manifest["kiso"]
        name = kiso["name"]
        if name in seen_names:
            log.warning("Duplicate tool name '%s' in %s (skipped)", name, entry)
            continue
        seen_names.add(name)

        tool_section = kiso.get("tool", {})
        args_schema = tool_section.get("args", {})
        env_decl = tool_section.get("env", {})
        session_secrets = tool_section.get("session_secrets", [])

        # usage_guide: local override file takes priority over toml default
        usage_guide_default = tool_section.get("usage_guide", "")
        override_path = entry / "usage_guide.local.md"
        if override_path.is_file():
            usage_guide = override_path.read_text().strip()
        else:
            usage_guide = usage_guide_default

        # Parse and validate consumes field
        raw_consumes = tool_section.get("consumes", [])
        consumes: list[str] = []
        if isinstance(raw_consumes, list):
            for val in raw_consumes:
                if isinstance(val, str) and val in _CONSUMES_VOCAB:
                    consumes.append(val)
                elif isinstance(val, str):
                    log.warning("Tool '%s': unknown consumes value '%s' (skipped)", name, val)

        info = {
            "name": kiso["name"],
            "summary": tool_section["summary"],
            "args_schema": args_schema,
            "env": env_decl,
            "session_secrets": session_secrets or [],
            "path": str(entry),
            "version": kiso.get("version", "0.0.0"),
            "description": kiso.get("description", ""),
            "usage_guide": usage_guide,
            "deps": kiso.get("deps", {}),
            "consumes": consumes,
        }
        missing = check_deps(info)
        info["healthy"] = len(missing) == 0
        info["missing_deps"] = missing
        tools.append(info)

    _tools_cache[resolved_dir] = (now, tools)
    return tools


def _tool_venv_bin(tool: dict) -> str:
    """Return the tool's ``.venv/bin`` path, or ``""`` if no path is set."""
    tool_path = tool.get("path", "")
    return str(Path(tool_path) / ".venv" / "bin") if tool_path else ""


def check_deps(tool: dict) -> list[str]:
    """Check [kiso.deps].bin entries with `which`. Returns list of missing binaries.

    Also searches the tool's own ``.venv/bin/`` directory, since pip-installed
    CLIs (e.g. ``playwright``) live there and the system PATH won't include it.
    """
    deps = tool.get("deps", {})
    bins = deps.get("bin", [])
    if not isinstance(bins, list):
        return []

    # Build an extended PATH that includes the tool's venv bin
    venv_bin = _tool_venv_bin(tool)
    search_path = (
        f"{venv_bin}:{os.environ.get('PATH', '')}" if venv_bin else None
    )

    missing: list[str] = []
    for b in bins:
        if not shutil.which(b, path=search_path):
            missing.append(b)
    return missing


def build_planner_tool_list(
    tools: list[dict],
    user_role: str = "admin",
    user_tools: str | list[str] | None = None,
) -> str:
    """Build the tool list text for the planner context.

    Filters tools based on user role and tools field:
    - admin: sees all tools
    - user with tools="*": sees all tools
    - user with tools=["a","b"]: sees only listed tools
    """
    if not tools:
        return ""

    # Filter by user access
    if user_role != "admin" and user_tools != "*":
        allowed = set(user_tools) if isinstance(user_tools, list) else set()
        tools = [t for t in tools if t["name"] in allowed]

    if not tools:
        return ""

    lines: list[str] = ["Available tools:"]
    for t in tools:
        if t.get("healthy") is False:
            missing = ", ".join(t.get("missing_deps", []))
            lines.append(
                f"- {t['name']} — {t['summary']}  [BROKEN — missing: {missing}. "
                f"Reinstall with: kiso tool remove {t['name']} && kiso tool install {t['name']}]"
            )
        else:
            lines.append(f"- {t['name']} — {t['summary']}")
        # show required args + top 3 optional to reduce prompt tokens
        args_schema = t.get("args_schema", {})
        required_args = {k: v for k, v in args_schema.items() if v.get("required")}
        optional_args = {k: v for k, v in args_schema.items() if not v.get("required")}
        _MAX_OPTIONAL_SHOWN = 3
        shown_optional = dict(list(optional_args.items())[:_MAX_OPTIONAL_SHOWN])
        omitted = len(optional_args) - len(shown_optional)
        for arg_name, arg_def in {**required_args, **shown_optional}.items():
            arg_type = arg_def.get("type", "string")
            req_str = "required" if arg_def.get("required") else "optional"
            default = arg_def.get("default")
            desc = arg_def.get("description", "")
            line = f"  args: {arg_name} ({arg_type}, {req_str}"
            if default is not None:
                line += f", default={default}"
            line += f"): {desc}"
            lines.append(line)
        if omitted > 0:
            lines.append(f"  ({omitted} more optional args)")

        guide = t.get("usage_guide", "")
        if guide:
            lines.append(f"  guide: {guide}")

    # File processing section — auto-generated from consumes declarations
    type_tools: dict[str, list[str]] = {}
    for t in tools:
        for ctype in t.get("consumes", []):
            entry_text = t["name"]
            # Short summary (first clause before ' — ' or first 40 chars)
            summary = t.get("summary", "")
            if " — " in summary:
                short = summary.split(" — ")[0]
            elif len(summary) > 40:
                short = summary[:37] + "..."
            else:
                short = summary
            if short:
                entry_text = f"{t['name']} ({short})"
            type_tools.setdefault(ctype, []).append(entry_text)

    if type_tools:
        lines.append("")
        lines.append("File processing (match session workspace files to these tools):")
        for ctype in sorted(type_tools):
            tool_list = ", ".join(type_tools[ctype])
            lines.append(f"- {ctype} files → {tool_list}")

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


_ARG_ALIASES: dict[str, str] = {
    "selector": "element",
    "text": "value",
    "query": "value",
}


def auto_correct_tool_args(args: dict, args_schema: dict) -> dict:
    """Fix common LLM arg name hallucinations. Returns corrected copy."""
    corrected = dict(args)
    for alias, canonical in _ARG_ALIASES.items():
        if alias in corrected and canonical not in corrected and canonical in args_schema:
            corrected[canonical] = corrected.pop(alias)
    return corrected


def validate_tool_args(args: dict, args_schema: dict) -> list[str]:
    """Validate parsed tool args against the schema. Returns list of errors."""
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


def build_tool_input(
    tool: dict,
    args: dict,
    session: str,
    workspace: str,
    session_secrets: dict[str, str] | None = None,
    plan_outputs: list[dict] | None = None,
) -> dict:
    """Build the input JSON dict for a tool subprocess."""
    # Scope session_secrets to only declared ones
    declared = set(tool.get("session_secrets", []))
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


def build_tool_env(tool: dict) -> dict[str, str]:
    """Build the environment dict for a tool subprocess.

    Includes PATH, the base LLM API key (when set), and any tool-specific
    deploy secret env vars (KISO_TOOL_{NAME}_{KEY}).
    """
    venv_bin = _tool_venv_bin(tool)
    sys_path = os.environ.get("PATH", "/usr/bin:/bin")
    env: dict[str, str] = {
        "PATH": f"{venv_bin}:{sys_path}" if venv_bin else sys_path,
    }

    # Propagate base LLM key so tools can fall back to it.
    llm_key = os.environ.get(LLM_API_KEY_ENV)
    if llm_key:
        env[LLM_API_KEY_ENV] = llm_key

    env_decl = tool.get("env", {})
    tool_name = tool["name"]
    for key, decl in env_decl.items():
        var_name = _env_var_name(tool_name, key)
        value = os.environ.get(var_name)
        if value is not None:
            env[var_name] = value
        elif isinstance(decl, dict) and decl.get("required"):
            log.warning("Env var %s not set (required by tool %s)", var_name, tool_name)

    return env
