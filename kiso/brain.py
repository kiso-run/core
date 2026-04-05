"""Planner brain — builds context, calls LLM, validates plan."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

import aiosqlite

from kiso.config import Config, KISO_DIR, setting_bool, setting_int
from kiso.llm import LLMBudgetExceeded, LLMError, LLMStallError, call_llm
from kiso.registry import get_registry_tools
from kiso.security import fence_content
from kiso.connectors import discover_connectors
from kiso.recipe_loader import discover_recipes, build_planner_recipe_list
from kiso.tools import (
    discover_tools,
    build_planner_tool_list,
    validate_tool_args,
    validate_tool_args_semantic,
)
from kiso.store import (
    _normalize_entity_name,
    delete_facts, get_all_entities, get_all_tags, get_facts, get_kv, get_pending_items,
    get_behavior_facts, get_recent_messages, get_safety_facts, get_session, search_facts,
    search_facts_by_entity, search_facts_by_tags, search_facts_scored,
    set_kv, update_fact_content,
)
from kiso.sysenv import get_system_env, build_system_env_essential, build_system_env_section, build_install_context, build_user_settings_text

log = logging.getLogger(__name__)

# Task type constants
TASK_TYPE_EXEC = "exec"
TASK_TYPE_MSG = "msg"
TASK_TYPE_TOOL = "tool"
TASK_TYPE_SEARCH = "search"
TASK_TYPE_REPLAN = "replan"
TASK_TYPES: frozenset[str] = frozenset({
    TASK_TYPE_EXEC, TASK_TYPE_MSG, TASK_TYPE_TOOL, TASK_TYPE_SEARCH, TASK_TYPE_REPLAN,
})

# Review status constants
REVIEW_STATUS_OK = "ok"
REVIEW_STATUS_REPLAN = "replan"
REVIEW_STATUS_STUCK = "stuck"
REVIEW_STATUSES: frozenset[str] = frozenset({
    REVIEW_STATUS_OK, REVIEW_STATUS_REPLAN, REVIEW_STATUS_STUCK,
})

# Curator verdict constants
CURATOR_VERDICT_PROMOTE = "promote"
CURATOR_VERDICT_ASK = "ask"
CURATOR_VERDICT_DISCARD = "discard"
CURATOR_VERDICTS: frozenset[str] = frozenset({
    CURATOR_VERDICT_PROMOTE, CURATOR_VERDICT_ASK, CURATOR_VERDICT_DISCARD,
})

# Example values for tool args types (used in validation error messages)
_TYPE_EXAMPLES: dict = {"string": "value", "int": 1, "float": 1.0, "bool": True}

# Worker phase constants
WORKER_PHASE_CLASSIFYING = "classifying"
WORKER_PHASE_PLANNING = "planning"
WORKER_PHASE_EXECUTING = "executing"
WORKER_PHASE_IDLE = "idle"
WORKER_PHASES: frozenset[str] = frozenset({
    WORKER_PHASE_CLASSIFYING, WORKER_PHASE_PLANNING,
    WORKER_PHASE_EXECUTING, WORKER_PHASE_IDLE,
})

# Fact constants
_MAX_MESSENGER_FACTS = 50  # cap on facts injected into the messenger LLM context
_MESSENGER_RETRY_BACKOFF: float = 1.0  # seconds between retries (0 in tests)
_MAX_MESSENGER_RETRIES = 2  # max retries on transient LLM errors
_VALID_FACT_CATEGORIES: frozenset[str] = frozenset({"general", "project", "tool", "user", "system", "safety", "behavior"})
_ENTITY_KINDS: frozenset[str] = frozenset({"website", "company", "tool", "person", "project", "concept", "system"})

# In-flight message classification
INFLIGHT_CATEGORIES: frozenset[str] = frozenset({"stop", "update", "independent", "conflict"})
INFLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": sorted(INFLIGHT_CATEGORIES)},
        "reason": {"type": "string"},
    },
    "required": ["category", "reason"],
}


def _build_strict_schema(name: str, properties: dict, required: list[str]) -> dict:
    """Build a strict JSON-schema response format dict for LLM calls."""
    return {"type": "json_schema", "json_schema": {
        "name": name, "strict": True,
        "schema": {"type": "object", "properties": properties,
                   "required": required, "additionalProperties": False},
    }}


def _join_or_empty(items: list, fmt: Callable = lambda x: f"- {x}") -> str:
    """Join items with newlines using fmt, or return empty string if empty."""
    return "\n".join(fmt(item) for item in items) if items else ""


def _format_message_history(messages: list[dict]) -> str:
    """Format a list of message dicts into '[role] user: content' lines."""
    return "\n".join(
        f"[{m['role']}] {m.get('user') or 'system'}: {m['content']}"
        for m in messages
    )


_INSTALL_KEYWORDS = frozenset({
    "install", "installa", "installo", "installare", "installer",
    "needs_install", "tool install", "connector install",
})
_APPROVAL_KEYWORDS = frozenset({
    "sì", "si", "yes", "ok", "vai", "do it", "proceed", "confirma",
    "installa", "install",
})
_INSTALL_MODE_NONE = "none"
_INSTALL_MODE_KISO_TOOL = "kiso_tool"
_INSTALL_MODE_PYTHON_LIB = "python_lib"
_INSTALL_MODE_SYSTEM_PKG = "system_pkg"
_INSTALL_TARGET_RE = re.compile(
    r"\b(?:install|installa|installare|installer)\b"
    r"(?:\s+(?:the|a|an|il|lo|la|i|gli|le|un|una))?"
    r"(?:\s+(?:kiso\s+tool|tool|plugin|package|pkg|pacchetto|libreria|library|module|modulo|python\s+package|python\s+library|python\s+module|system\s+package|pacchetto\s+di\s+sistema))?"
    r"\s+([a-z0-9][a-z0-9._+-]{0,63})\b",
    re.IGNORECASE,
)
_SYSTEM_INSTALL_HINT_RE = re.compile(
    r"\b(?:apt(?:-get)?|dnf|yum|apk|pacman|zypper|brew|pkg manager|package manager|system package|pacchetto di sistema)\b",
    re.IGNORECASE,
)
_PYTHON_INSTALL_HINT_RE = re.compile(
    r"\b(?:uv\s+pip|pip|pypi|python\s+package|python\s+library|python\s+module|pacchetto python|libreria python|modulo python)\b",
    re.IGNORECASE,
)
_COMMON_PYTHON_PACKAGES = frozenset({
    "aiohttp", "aiosqlite", "anthropic", "beautifulsoup4", "black", "bs4",
    "celery", "click", "django", "fastapi", "flask", "httpx", "jinja2",
    "langchain", "lxml", "matplotlib", "numpy", "openai", "pandas",
    "playwright", "pydantic", "pytest", "requests", "rich", "scipy",
    "seaborn", "sqlalchemy", "streamlit", "tenacity", "tomli", "uvicorn",
})


def _compress_install_turns(lines: list[str]) -> list[str]:
    """Compress install proposal→approval→result sequences in recent context.

    Detects consecutive [kiso] install proposal + [user] approval + [kiso]
    result and replaces them with a single "[install completed]" line,
    keeping the original user request that triggered the proposal.

    Non-install messages pass through unchanged.
    """
    if len(lines) < 3:
        return lines

    result: list[str] = []
    i = 0
    while i < len(lines):
        # Look for pattern: [kiso] install proposal + [user] approval + [kiso] result
        if (
            i + 2 < len(lines)
            and lines[i].startswith("[kiso]")
            and lines[i + 1].startswith("[user]")
            and lines[i + 2].startswith("[kiso]")
        ):
            kiso_text = lines[i].lower()
            user_text = lines[i + 1].lower()
            kiso_result = lines[i + 2].lower()

            is_proposal = any(kw in kiso_text for kw in _INSTALL_KEYWORDS)
            is_approval = any(kw in user_text for kw in _APPROVAL_KEYWORDS)
            is_result = "install" in kiso_result or "replan" in kiso_result

            if is_proposal and is_approval and is_result:
                # Compress: extract tool name heuristically
                result.append("[install completed] tool installed and available.")
                i += 3
                continue

        result.append(lines[i])
        i += 1

    return result


def _parse_registry_hint_names(registry_hints: str) -> frozenset[str]:
    """Extract tool names from sysenv registry_hints text."""
    if not registry_hints:
        return frozenset()
    return frozenset(
        part.split("(")[0].strip().lower()
        for part in registry_hints.split(";")
        if part.strip()
    )


def _extract_install_target(message: str) -> str | None:
    """Best-effort package/tool target extraction from install requests."""
    match = _INSTALL_TARGET_RE.search(message)
    if not match:
        return None
    return match.group(1).lower()


def _classify_install_mode(
    message: str,
    sys_env: dict,
    *,
    installed_tool_names: "list[str] | set[str] | None" = None,
    registry_hint_names: "set[str] | frozenset[str] | None" = None,
) -> dict[str, str]:
    """Deterministically route install-family requests before planning."""
    msg_lower = message.lower()
    if not any(kw in msg_lower for kw in _INSTALL_KEYWORDS):
        return {"mode": _INSTALL_MODE_NONE}

    target = _extract_install_target(message)
    if not target:
        return {"mode": _INSTALL_MODE_NONE}

    hint_names = {n.lower() for n in (registry_hint_names or set())}
    installed_names = {n.lower() for n in (installed_tool_names or set())}
    if target in hint_names or target in installed_names:
        return {
            "mode": _INSTALL_MODE_KISO_TOOL,
            "target": target,
            "reason": "target matches kiso tool context",
        }

    if _SYSTEM_INSTALL_HINT_RE.search(msg_lower):
        return {
            "mode": _INSTALL_MODE_SYSTEM_PKG,
            "target": target,
            "reason": "user explicitly requested a system package manager flow",
        }

    if _PYTHON_INSTALL_HINT_RE.search(msg_lower):
        return {
            "mode": _INSTALL_MODE_PYTHON_LIB,
            "target": target,
            "reason": "user explicitly referenced Python package installation",
        }

    if target in _COMMON_PYTHON_PACKAGES:
        return {
            "mode": _INSTALL_MODE_PYTHON_LIB,
            "target": target,
            "reason": "target matches common Python package catalog",
        }

    pkg_manager = (sys_env.get("os") or {}).get("pkg_manager")
    available = {b.lower() for b in sys_env.get("available_binaries") or []}
    if pkg_manager:
        return {
            "mode": _INSTALL_MODE_SYSTEM_PKG,
            "target": target,
            "reason": f"no kiso/Python signal; fallback to system package manager ({pkg_manager})",
        }
    if "uv" in available or "python3" in available or "python" in available:
        return {
            "mode": _INSTALL_MODE_PYTHON_LIB,
            "target": target,
            "reason": "no system package manager available; fallback to Python package flow",
        }
    return {"mode": _INSTALL_MODE_NONE}


def _build_install_mode_context(route: dict[str, str], sys_env: dict) -> str:
    """Format deterministic install routing for planner context."""
    mode = route.get("mode", _INSTALL_MODE_NONE)
    if mode == _INSTALL_MODE_NONE:
        return ""
    target = route.get("target", "unknown")
    pkg_manager = (sys_env.get("os") or {}).get("pkg_manager") or "package manager"
    lines = [f"Target: {target}", f"Mode: {mode}"]
    if mode == _INSTALL_MODE_KISO_TOOL:
        lines.append("Route: kiso tool proposal — set needs_install + approval msg only.")
        lines.append("Do not use apt-get or uv pip install for this target.")
    elif mode == _INSTALL_MODE_PYTHON_LIB:
        lines.append(f"Route: Python library — exec `uv pip install {target}`.")
        lines.append("Do not set needs_install and do not use the system package manager.")
    elif mode == _INSTALL_MODE_SYSTEM_PKG:
        lines.append(f"Route: system package — exec the {pkg_manager} install flow.")
        lines.append("Do not set needs_install and do not use uv pip install.")
    reason = route.get("reason")
    if reason:
        lines.append(f"Reason: {reason}.")
    return "\n".join(lines)


def build_recent_context(
    messages: list[dict], *, max_chars: int = 0, kiso_truncate: int = 200,
) -> str:
    """Unified conversation context builder for all LLM roles.

    Formats messages as::

        [user] root: vai su guidance.studio
        [kiso] Per navigare serve il browser tool. Vuoi che lo installi?
        [user] root: oh yeah

    User messages use ``[user] {username}``. Assistant/system messages use
    ``[kiso]`` with content truncated to *kiso_truncate* chars (kiso responses
    can be very long; the gist is enough for context).

    Install proposal→approval→result sequences are compressed to reduce
    context noise after tool installation cycles.

    When *max_chars* > 0, older messages are dropped to stay within budget
    (most recent messages preserved).
    """
    if not messages:
        return ""

    lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        if role in ("assistant", "system"):
            content = m.get("content", "")
            if kiso_truncate and len(content) > kiso_truncate:
                content = content[:kiso_truncate] + "..."
            lines.append(f"[kiso] {content}")
        else:
            user = m.get("user") or "user"
            lines.append(f"[user] {user}: {m.get('content', '')}")

    # Compress install turns to reduce noise after install cycles
    lines = _compress_install_turns(lines)

    if max_chars > 0:
        # Keep most recent messages within budget
        result_lines: list[str] = []
        total = 0
        for line in reversed(lines):
            line_len = len(line) + 1  # +1 for newline
            if total + line_len > max_chars and result_lines:
                break
            result_lines.append(line)
            total += line_len
        result_lines.reverse()
        return "\n".join(result_lines)

    return "\n".join(lines)


def _format_pending_items(pending: list[dict]) -> str:
    """Format pending question dicts into '- content' lines."""
    return _join_or_empty(pending, lambda p: f"- {p['content']}")


def _strip_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) that some models wrap around JSON."""
    s = text.strip()
    if s.startswith("```"):
        # Remove opening fence line
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _repair_json(text: str) -> str:
    """Best-effort JSON repair: strip fences, fix trailing commas."""
    s = _strip_fences(text)
    return _TRAILING_COMMA_RE.sub(r"\1", s)


_INSTALL_CMD_RE = re.compile(
    r"kiso\s+(tool|skill|connector)\s+install", re.IGNORECASE,
)
# Extract plugin name from "kiso tool install <name>" for registry validation.
_INSTALL_NAME_RE = re.compile(
    r"kiso\s+(?:tool|skill|connector)\s+install\s+(\S+)", re.IGNORECASE,
)
# Detect external git URLs — these bypass registry name validation.
_GIT_URL_RE = re.compile(r"https?://|git@|\.git\b", re.IGNORECASE)
# Exec details mentioning pip for package installation without uv prefix.
# Catches: "pip install X", "install X using pip", "use pip to install".
_PIP_INSTALL_RE = re.compile(
    r"\bpip\s+install\b|\binstall\b.*\busing\s+pip\b|\buse\s+pip\b.*\binstall\b",
    re.IGNORECASE,
)
_UV_PIP_RE = re.compile(r"\buv\s+pip\b", re.IGNORECASE)
# marker substring in validation errors for uninstalled-tool detection.
# Used both when generating the error (validate_plan) and detecting it
# (_retry_llm_with_validation).  Keep in sync.
_TOOL_NOT_INSTALLED_MARKER = "is not installed"
_TOOL_UNAVAILABLE_MARKER = "not available — not installed and not in the registry"

_ABS_PATH_RE = re.compile(r"(/[a-zA-Z0-9_./-]+)")


def check_safety_rules(detail: str, safety_facts: list[dict]) -> str | None:
    """Check exec task detail against safety rules.

    Returns a rejection reason if the detail references a protected
    resource mentioned in a safety fact, or None if safe.

    Conservative matching: only triggers when a safety fact contains an
    absolute path AND the detail references the same path (substring match).
    """
    if not safety_facts or not detail:
        return None
    detail_lower = detail.lower()
    for fact in safety_facts:
        content = fact.get("content", "")
        paths = _ABS_PATH_RE.findall(content)
        for path in paths:
            path_lower = path.lower()
            if path_lower in detail_lower:
                return (
                    f"Blocked by safety rule: \"{content}\" "
                    f"(detail references protected path {path})"
                )
    return None


_PLUGIN_DISCOVERY_RE = re.compile(
    r"(?:tool|skill|connector|plugin).*(?:registr|install|discover|find|search|browse|cercar)"
    r"|(?:registr|kiso).*(?:tool|skill|connector|plugin)",
    re.IGNORECASE,
)


def _is_plugin_discovery_search(detail: str) -> bool:
    """Return True if detail looks like a plugin discovery search query."""
    return bool(_PLUGIN_DISCOVERY_RE.search(detail))

async def _retry_llm_with_validation(
    config: Config,
    role: str,
    messages: list[dict],
    schema: dict,
    validate_fn,
    error_class: type[Exception],
    error_noun: str,
    session: str = "",
    on_retry: Callable[[int, int, str], None] | None = None,
    fallback_model: str | None = None,
) -> dict:
    """Generic retry loop: call LLM, parse JSON, validate, retry on errors.

    Uses two separate retry budgets:
    - ``max_llm_retries`` (config) for LLM-level failures (timeout, stall, HTTP).
    - ``max_validation_retries`` (config) for JSON parse / schema validation.

    Args:
        config: App config.
        role: LLM model route name (e.g. "planner", "reviewer", "curator").
        messages: Initial message list (mutated in-place with retries).
        schema: JSON schema for structured output.
        validate_fn: Callable(parsed_dict) → list[str] errors.
        error_class: Exception type to raise on exhaustion.
        error_noun: Human noun for error messages (e.g. "Plan", "Review").
        session: Session name for LLM call tracking.
        on_retry: Optional callback(attempt, max_attempts, reason) called before
            each retry attempt (not called on the first attempt).
        fallback_model: Optional model to switch to when primary model exhausts
            LLM retries.  Only used once — after fallback, exhaustion
            raises normally.

    Returns:
        The validated parsed dict.
    """
    max_validation_retries = int(config.settings["max_validation_retries"])
    max_llm_retries = int(config.settings.get("max_llm_retries", 3))
    max_total = max_validation_retries + max_llm_retries

    last_errors: list[str] = []
    prev_error_set: frozenset[str] = frozenset()  # track repeated identical errors
    repeat_count: int = 0
    llm_errors = 0
    validation_errors = 0
    attempt = 0
    active_model: str | None = None  # None means use default from config
    saw_uninstalled_tool = False  # track uninstalled-tool validation errors

    while attempt < max_total:
        attempt += 1

        if last_errors:
            error_lines = [f"- {e}" for e in last_errors]
            # escalate after 2+ identical error patterns
            if repeat_count >= 2:
                error_lines.append(
                    "\nIMPORTANT: You have made this same error "
                    f"{repeat_count} times. Read the error message above "
                    "carefully and apply the exact fix described."
                )
            error_feedback = (
                f"Your {error_noun.lower()} has errors:\n"
                + "\n".join(error_lines)
                + f"\nFix these and return the corrected {error_noun.lower()}."
            )
            messages.append({"role": "user", "content": error_feedback})

        try:
            raw = await call_llm(
                config, role, messages, response_format=schema,
                session=session, model_override=active_model,
            )
        except LLMStallError as e:
            # stall = provider-level issue — retry on same model is futile.
            # Switch to fallback immediately without consuming retry budget.
            if fallback_model and active_model != fallback_model:
                log.warning("SSE stall on %s, switching to fallback %s", role, fallback_model)
                active_model = fallback_model
                llm_errors = 0
                max_total += max_llm_retries
                if on_retry is not None:
                    on_retry(attempt + 1, max_total, f"SSE stall — switching to fallback: {fallback_model}")
                continue
            # No fallback available — raise immediately (don't retry)
            exc = error_class(f"LLM stall with no fallback: {e}")
            exc.last_errors = last_errors
            raise exc
        except LLMError as e:
            llm_errors += 1
            # circuit breaker open → switch to fallback immediately
            _is_cb = "Circuit breaker open" in str(e)
            if _is_cb and fallback_model and active_model != fallback_model:
                log.warning("Circuit breaker open; switching to fallback %s", fallback_model)
                active_model = fallback_model
                llm_errors = 0
                max_total += max_llm_retries
                if on_retry is not None:
                    on_retry(attempt + 1, max_total, f"Switching to fallback model: {fallback_model}")
                continue
            log.warning("LLM error (%d/%d LLM retries): %s", llm_errors, max_llm_retries, e)
            if llm_errors >= max_llm_retries:
                # switch to fallback model instead of raising
                if fallback_model and active_model != fallback_model:
                    log.warning(
                        "Primary model exhausted %d retries; switching to fallback %s",
                        llm_errors, fallback_model,
                    )
                    active_model = fallback_model
                    llm_errors = 0
                    max_total += max_llm_retries  # extend budget for fallback
                    if on_retry is not None:
                        on_retry(attempt + 1, max_total, f"Switching to fallback model: {fallback_model}")
                    continue
                exc = error_class(f"LLM call failed after {llm_errors} attempts: {e}")
                exc.last_errors = last_errors  # preserve for auto-correction
                raise exc
            # notify caller before retry
            if on_retry is not None:
                on_retry(attempt + 1, max_total, str(e))
            continue

        try:
            result = json.loads(_repair_json(raw))
        except json.JSONDecodeError as e:
            validation_errors += 1
            log.warning("%s returned invalid JSON (%d/%d validation retries): %s",
                        error_noun, validation_errors, max_validation_retries, e)
            if validation_errors >= max_validation_retries:
                exc = error_class(
                    f"{error_noun} validation failed after {validation_errors} attempts: {last_errors}"
                )
                exc.last_errors = last_errors
                raise exc
            last_errors = [
                f"Invalid JSON at line {e.lineno} col {e.colno}: {e.msg} — "
                "return ONLY the JSON object, no markdown, no trailing commas"
            ]
            messages.append({"role": "assistant", "content": raw})
            if on_retry is not None:
                on_retry(attempt + 1, max_total, f"JSON parse error: {e}")
            continue

        errors = validate_fn(result)
        if not errors:
            log.info("%s accepted (attempt %d)", error_noun, attempt)
            # propagate uninstalled-tool signal on the result dict
            result["_saw_uninstalled_tool"] = saw_uninstalled_tool
            return result

        validation_errors += 1
        log.warning("%s validation failed (%d/%d validation retries): %s",
                    error_noun, validation_errors, max_validation_retries, errors)
        if validation_errors >= max_validation_retries:
            exc = error_class(
                f"{error_noun} validation failed after {validation_errors} attempts: {errors}"
            )
            exc.last_errors = errors
            raise exc

        # detect uninstalled-tool errors for install-proposal detection
        if not saw_uninstalled_tool and any(_TOOL_NOT_INSTALLED_MARKER in e for e in errors):
            saw_uninstalled_tool = True

        # track consecutive identical errors for escalation
        error_set = frozenset(errors)
        if error_set == prev_error_set:
            repeat_count += 1
        else:
            prev_error_set = error_set
            repeat_count = 1
        last_errors = errors
        messages.append({"role": "assistant", "content": raw})
        if on_retry is not None:
            on_retry(attempt + 1, max_total, f"Validation: {errors}")

    # Safety net: should not reach here normally
    exc = error_class(
        f"{error_noun} failed after {attempt} total attempts"
    )
    exc.last_errors = last_errors
    raise exc


PLAN_SCHEMA: dict = _build_strict_schema("plan", {
    "goal": {"type": "string"},
    "secrets": {"anyOf": [
        {"type": "array", "items": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"], "additionalProperties": False,
        }},
        {"type": "null"},
    ]},
    "tasks": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["exec", "msg", "tool", "search", "replan"]},
            "detail": {"type": "string"},
            "tool": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "args": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "expect": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "group": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
        },
        "required": ["type", "detail", "tool", "args", "expect"],
        "additionalProperties": False,
    }},
    "extend_replan": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "needs_install": {"anyOf": [
        {"type": "array", "items": {"type": "string"}},
        {"type": "null"},
    ]},
    "knowledge": {"anyOf": [
        {"type": "array", "items": {"type": "string"}},
        {"type": "null"},
    ]},
}, ["goal", "secrets", "tasks", "extend_replan", "needs_install", "knowledge"])


REVIEW_SCHEMA: dict = _build_strict_schema("review", {
    "status": {"type": "string", "enum": ["ok", "replan", "stuck"]},
    "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "learn": {"anyOf": [
        {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        {"type": "null"},
    ]},
    "retry_hint": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
}, ["status", "reason", "learn", "retry_hint", "summary"])


BRIEFER_SCHEMA: dict = _build_strict_schema("briefing", {
    "modules": {"type": "array", "items": {"type": "string"}},
    "tools": {"type": "array", "items": {"type": "string"}},
    "exclude_recipes": {"type": "array", "items": {"type": "string"}},
    "context": {"type": "string"},
    "output_indices": {"type": "array", "items": {"type": "integer"}},
    "relevant_tags": {"type": "array", "items": {"type": "string"}},
    "relevant_entities": {"type": "array", "items": {"type": "string"}},
}, ["modules", "tools", "exclude_recipes", "context", "output_indices", "relevant_tags", "relevant_entities"])

# Available prompt modules for reviewer (heuristic selection, no briefer).
# core is always included; these are optional additions.
REVIEWER_MODULES: frozenset[str] = frozenset({
    "rules", "learn_quality", "compliance",
})

# Available prompt modules for curator (heuristic selection, no briefer).
CURATOR_MODULES: frozenset[str] = frozenset({
    "entity_assignment", "tag_reuse",
})

# Available prompt modules that the briefer can select.
# core is always included and NOT listed here — these are optional additions.
BRIEFER_MODULES: frozenset[str] = frozenset({
    "planning_rules", "kiso_native", "tools_rules",
    "web", "replan", "tool_recovery", "data_flow",
    "kiso_commands", "user_mgmt", "plugin_install",
})
_BRIEFER_MODULE_DESCRIPTIONS: dict[str, str] = {
    "planning_rules": "task ordering, expect rules, multi-step plans",
    "kiso_native": "kiso-first policy, registry checking",
    "tools_rules": "tool usage rules, atomic operations",
    "web": "URLs, websites, browser tool rules",
    "data_flow": "file-based data flow for large outputs",
    "replan": "re-planning after failure, extend flag",
    "tool_recovery": "broken tool reinstall procedure",
    "kiso_commands": "kiso CLI (tool/connector/env/user mgmt)",
    "user_mgmt": "user/alias management, role permissions",
    "plugin_install": "plugin discovery and installation",
}
_BRIEFER_MODULES_STR = "\n".join(
    f"- {name}: {_BRIEFER_MODULE_DESCRIPTIONS[name]}"
    for name in sorted(BRIEFER_MODULES)
)


class BrieferError(Exception):
    """Briefer generation failure."""


class ReviewError(Exception):
    """Review validation or generation failure."""


class PlanError(Exception):
    """Plan validation or generation failure."""


_ROLES_DIR = Path(__file__).parent / "roles"
_prompt_cache: dict[str, str] = {}


def _load_system_prompt(role: str) -> str:
    """Load system prompt: user override first, then package default.

    Results are cached in-process. Call :func:`invalidate_prompt_cache` to
    force a reload (e.g. after the user edits a role file).
    """
    if role in _prompt_cache:
        return _prompt_cache[role]
    # User override
    user_path = KISO_DIR / "roles" / f"{role}.md"
    if user_path.exists():
        text = user_path.read_text()
        _prompt_cache[role] = text
        return text
    # Package default
    pkg_path = _ROLES_DIR / f"{role}.md"
    if pkg_path.exists():
        text = pkg_path.read_text()
        _prompt_cache[role] = text
        return text
    raise FileNotFoundError(f"No prompt found for role '{role}'")


def invalidate_prompt_cache() -> None:
    """Clear the in-process system-prompt cache."""
    _prompt_cache.clear()


_MODULE_MARKER_RE = re.compile(r"<!--\s*MODULE:\s*(\w+)\s*-->")
_ANSWER_IN_LANG_RE = re.compile(r"^Answer in (\w[\w\s]*)\.")


def _load_modular_prompt(role: str, modules: list[str]) -> str:
    """Load a role prompt, returning only core + selected modules.

    The prompt file must use ``<!-- MODULE: name -->`` markers to delimit
    sections.  The ``core`` module is always included.  If no markers are
    found the full prompt is returned unchanged (backward compat).
    """
    full_text = _load_system_prompt(role)
    parts = _MODULE_MARKER_RE.split(full_text)
    # If no markers found, return full prompt
    if len(parts) <= 1:
        return full_text

    # parts alternates: [preamble, name1, text1, name2, text2, ...]
    # preamble (parts[0]) is discarded — prompt files must start with a marker.
    wanted = {"core"} | set(modules)
    sections: list[str] = []
    for i in range(1, len(parts), 2):
        name = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if name in wanted:
            sections.append(body)
    stripped = [s.strip() for s in sections]
    return "\n".join(s for s in stripped if s)


def _build_messages(system_prompt: str, user_content: str) -> list[dict]:
    """Assemble the canonical [system, user] message pair used by all LLM roles."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _add_section(parts: list[str], name: str, content: str) -> None:
    """Append a ``## {name}`` section to *parts* if *content* is non-empty."""
    if content:
        parts.append(f"## {name}\n{content}")


def _validate_plan_structure(
    plan: dict, max_tasks: int | None, is_replan: bool,
) -> tuple[list[str], list[dict]]:
    """Check top-level plan fields and strip extend_replan from initial plans.

    Returns (errors, tasks) so callers can short-circuit on structural failures.
    """
    if not is_replan:
        plan.pop("extend_replan", None)
    errors: list[str] = []
    tasks = plan.get("tasks", [])
    if not tasks:
        errors.append("tasks list must not be empty")
    elif max_tasks is not None and len(tasks) > max_tasks:
        errors.append(f"Plan has {len(tasks)} tasks, max allowed is {max_tasks}")
    return errors, tasks


# Exec details starting with these phrases are analytical (not shell-translatable).
# Exception: if the detail also contains a `/` path or known binary, allow it
# (e.g., "Verify that /tmp/output.txt exists" → `test -f /tmp/output.txt`).
_NON_ACTIONABLE_PREFIXES = (
    "check the content", "identify ", "determine ", "analyze ",
    "validate the ", "verify the content", "inspect the content",
    "review the ", "understand ", "evaluate ",
)
_DIRECT_TOOL_EXEC_VERB_RE = r"(?:use|run|invoke|launch|ask|have)"
_DIRECT_TOOL_EXEC_SUFFIX_RE = r"(?:to|for|on|with|against)\b"
_ACTION_TO_USER_RE = re.compile(
    r"\b("
    r"tell me|send me|show me|report back|reply with|let me know|"
    r"tell the user|send the user|show the user|report to the user|"
    r"dimmi|mandami|mostrami|fammi sapere|inviami|dillo all'utente|"
    r"manda all'utente|mostra all'utente|riporta all'utente"
    r")\b",
    re.IGNORECASE,
)


def _is_non_actionable_exec(detail: str) -> bool:
    """Return True if exec detail is analytical rather than shell-actionable."""
    lower = detail.lower().strip()
    if not any(lower.startswith(p) for p in _NON_ACTIONABLE_PREFIXES):
        return False
    # Allow if detail contains a concrete path or known binary
    if "/" in detail:
        return False
    return True


def _find_direct_tool_exec(
    detail: str, installed_skills: list[str] | None,
) -> str | None:
    """Return tool name if exec detail tries to use an installed kiso tool.

    This is intentionally narrow: it catches routing mistakes like
    "Use aider to write ..." or "Run browser on https://...".
    Normal shell tasks mentioning unrelated words must keep passing.
    """
    if not installed_skills:
        return None

    lower = (detail or "").lower()
    if "kiso tool " in lower or "kiso connector " in lower:
        return None
    if _INSTALL_CMD_RE.search(lower):
        return None

    for name in sorted(installed_skills, key=len, reverse=True):
        pattern = (
            rf"\b{_DIRECT_TOOL_EXEC_VERB_RE}\s+{re.escape(name.lower())}\s+"
            rf"{_DIRECT_TOOL_EXEC_SUFFIX_RE}"
        )
        if re.search(pattern, lower):
            return name
    return None


def _mentions_user_delivery(detail: str) -> bool:
    """Return True when an action-task detail includes user-delivery wording."""
    return bool(_ACTION_TO_USER_RE.search(detail or ""))


def _validate_plan_tasks(
    tasks: list[dict],
    installed_skills: list[str] | None,
    installed_skills_info: dict[str, dict] | None,
    install_approved: bool = False,
    registry_hint_names: frozenset[str] | None = None,
) -> list[str]:
    """Check per-task rules: type, detail, expect, args, tool validation."""
    errors: list[str] = []
    replan_count = 0
    for i, task in enumerate(tasks, 1):
        t = task.get("type")
        if t not in TASK_TYPES:
            errors.append(f"Task {i}: unknown type {t!r}")
            continue
        if t in (TASK_TYPE_EXEC, TASK_TYPE_TOOL, TASK_TYPE_SEARCH) and task.get("expect") is None:
            errors.append(
                f"Task {i}: {t} task must have expect describing WHAT RESULT you need "
                f"(e.g., 'list of search results', 'file created successfully')"
            )
        detail = task.get("detail") or ""
        if t == TASK_TYPE_EXEC and len(detail) > 500:
            errors.append(
                f"Task {i}: exec detail is {len(detail)} chars — too long. "
                f"Detail must be natural language intent, not embedded data or commands. "
                f"Save large data to files and reference the file path instead."
            )
        if t == TASK_TYPE_EXEC and _is_non_actionable_exec(detail):
            errors.append(
                f"Task {i}: exec detail is analytical, not actionable — "
                f"rewrite as a concrete shell command description "
                f"(e.g., 'Run kiso tool install browser')"
            )
        direct_tool_exec = _find_direct_tool_exec(detail, installed_skills)
        if t == TASK_TYPE_EXEC and direct_tool_exec:
            errors.append(
                f"Task {i}: exec detail directly routes installed tool '{direct_tool_exec}'. "
                f"Installed kiso tools must use type='tool' with tool='{direct_tool_exec}', "
                f"not type='exec'."
            )
        if t == TASK_TYPE_EXEC and _PIP_INSTALL_RE.search(detail) and not _UV_PIP_RE.search(detail):
            errors.append(
                f"Task {i}: use 'uv pip install' instead of bare 'pip install'. "
                f"Direct pip can corrupt the system environment."
            )
        if t in (TASK_TYPE_EXEC, TASK_TYPE_TOOL, TASK_TYPE_SEARCH) and _mentions_user_delivery(detail):
            errors.append(
                f"Task {i}: action task detail includes user-delivery wording. "
                f"Action tasks should do the work only; use a final msg task "
                f"to tell/send results to the user."
            )
        # M862: kiso plugin install for names not in registry (without git URL)
        if t == TASK_TYPE_EXEC and registry_hint_names is not None:
            name_match = _INSTALL_NAME_RE.search(detail)
            if name_match and not _GIT_URL_RE.search(detail):
                install_name = name_match.group(1).lower()
                if install_name not in registry_hint_names:
                    errors.append(
                        f"Task {i}: '{install_name}' is not in the kiso plugin registry. "
                        f"For system packages use the package manager (e.g. apt-get install), "
                        f"for Python libraries use uv pip install."
                    )
        if t == TASK_TYPE_MSG:
            for field in ("expect", "tool", "args"):
                if task.get(field) is not None:
                    errors.append(f"Task {i}: msg task must have {field} = null")
            # Language prefix ("Answer in X.") is NOT validated here —
            # _msg_task injects it at runtime from response_lang.
            # Only check that the detail has real content.
            msg_detail = (task.get("detail") or "").strip()
            cleaned = re.sub(r'^Answer in \w[\w\s]*\.\s*', '', msg_detail).strip()
            if len(cleaned) < 5:
                errors.append(
                    f"Task {i}: msg detail is empty or too short — "
                    f"must contain WHAT to tell the user"
                )
        if t == TASK_TYPE_SEARCH:
            if _is_plugin_discovery_search(task.get("detail", "")):
                errors.append(
                    f"Task {i}: search cannot be used for kiso plugin discovery. "
                    "If the tool name appears in registry_hints or "
                    "'Available Tools (not installed)', it is a kiso tool — "
                    "use the kiso_native install flow (set needs_install, "
                    "msg for approval). If it does NOT appear there, it is "
                    "not a kiso plugin — for system packages use the package "
                    "manager (e.g. apt-get install), for Python libraries "
                    "use uv pip install."
                )
            if task.get("tool") is not None:
                errors.append(f"Task {i}: search task must have tool = null")
        if t == TASK_TYPE_REPLAN:
            replan_count += 1
            if task.get("expect") is not None:
                errors.append(f"Task {i}: replan task must have expect = null")
            if task.get("tool") is not None:
                errors.append(f"Task {i}: replan task must have tool = null")
            if task.get("args") is not None:
                errors.append(f"Task {i}: replan task must have args = null")
            if i != len(tasks):
                errors.append(f"Task {i}: replan task can only be the last task")
        if t == TASK_TYPE_TOOL:
            tool_name = task.get("tool")
            if not tool_name:
                errors.append(f"Task {i}: tool task must have a non-null tool name")
            elif tool_name in (TASK_TYPE_EXEC, TASK_TYPE_MSG, TASK_TYPE_REPLAN):
                errors.append(
                    f"Task {i}: '{tool_name}' is a task TYPE, not a tool. "
                    f"Use type='{tool_name}' instead of type='tool' with "
                    f"tool='{tool_name}'."
                )
            elif tool_name in BRIEFER_MODULES:
                errors.append(
                    f"Task {i}: '{tool_name}' is a prompt module, not a tool. "
                    f"For shell commands, use type='exec'. For installed tools, "
                    f"use type='tool' with an actual tool name from the available list."
                )
            elif installed_skills is not None and tool_name not in installed_skills:
                available = ", ".join(sorted(installed_skills)) if installed_skills else "none"
                if install_approved:
                    errors.append(
                        f"Task {i}: tool '{tool_name}' is not installed. "
                        f"Available tools: {available}. "
                        f"You CANNOT use type=tool for uninstalled tools. "
                        f"Installation is approved — plan an exec task to install "
                        f"{tool_name} via the kiso CLI, then replan to use it."
                    )
                elif registry_hint_names and tool_name in registry_hint_names:
                    errors.append(
                        f"Task {i}: tool '{tool_name}' is not installed but IS "
                        f"available in the registry. If a built-in task type "
                        f"(e.g. search) can achieve the same goal, use that "
                        f"instead. Otherwise, plan a SINGLE msg task asking "
                        f"whether to install '{tool_name}', then end the plan."
                    )
                else:
                    errors.append(
                        f"Task {i}: tool '{tool_name}' is "
                        f"{_TOOL_UNAVAILABLE_MARKER}. Plan a SINGLE msg task "
                        f"informing the user that '{tool_name}' cannot be found "
                        f"in the public registry. If the user may have a private "
                        f"source, suggest providing a git URL or installation "
                        f"instructions. Do NOT plan any exec, search, or tool "
                        f"tasks referencing this tool."
                    )
            elif installed_skills_info and tool_name in installed_skills_info:
                args_raw = task.get("args") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except (json.JSONDecodeError, TypeError):
                    errors.append(f"Task {i}: tool args is not valid JSON")
                else:
                    schema = installed_skills_info[tool_name].get("args_schema", {})
                    arg_errors = validate_tool_args(args, schema)
                    semantic_errors = validate_tool_args_semantic(
                        installed_skills_info[tool_name],
                        args,
                        {
                            "phase": "planner",
                            "task_index": i,
                            "detail": task.get("detail"),
                            "expect": task.get("expect"),
                            "goal": task.get("goal"),
                        },
                    )
                    arg_errors.extend(semantic_errors)
                    if arg_errors:
                        # M1067: show only required args in example so the
                        # model focuses on what it MUST provide.
                        required_args = {
                            aname: _TYPE_EXAMPLES.get(adef.get("type", "string"), "value")
                            for aname, adef in schema.items()
                            if adef.get("required", False)
                        }
                        example_json = json.dumps(required_args)
                        errors.append(
                            f"Task {i}: tool '{tool_name}' args invalid: "
                            + "; ".join(arg_errors)
                            + f". Required args: '{example_json}'"
                        )

    if replan_count > 1:
        errors.append("A plan can have at most one replan task")

    return errors


# goal-plan mismatch — detect artifact requests with no exec/tool task.
_ARTIFACT_VERBS = frozenset({"create", "write", "generate", "build", "produce", "make"})
_ARTIFACT_NOUNS = frozenset({
    "file", "document", "script", "markdown", "csv", "report",
    "table", "spreadsheet", "config", "template", "page",
})


def _validate_plan_ordering(
    tasks: list[dict], is_replan: bool, install_approved: bool,
    has_needs_install: bool = False,
    has_knowledge: bool = False,
) -> list[str]:
    """Check cross-task ordering rules and install safety."""
    errors: list[str] = []

    # M1056: msg-only plans are rejected unless needs_install or knowledge is set.
    # Announce msgs BEFORE action tasks are fine — only pure msg-only is blocked.
    _DATA_TYPES = {TASK_TYPE_EXEC, TASK_TYPE_SEARCH, TASK_TYPE_TOOL, TASK_TYPE_REPLAN}
    has_action = any(t.get("type") in _DATA_TYPES for t in tasks)
    if not has_action and not is_replan:
        if not has_needs_install and not has_knowledge:
            errors.append(
                "Plan has only msg tasks — include at least one "
                "exec/tool/search task for action requests. "
                "Msg-only is valid only for kiso tool install proposals "
                "(set needs_install) or knowledge storage."
            )

    # install execs allowed in replans, when user approved in prior msg cycle,
    # or when user directly requested install (needs_install is empty — no proposal).
    # M979: only block when needs_install IS set (mixed propose+install in same plan).
    if not is_replan and not install_approved and has_needs_install:
        first_install_idx = next(
            (i for i, t in enumerate(tasks)
             if t.get("type") == TASK_TYPE_EXEC and _INSTALL_CMD_RE.search(t.get("detail", ""))),
            None,
        )
        if first_install_idx is not None:
            errors.append(
                f"Task {first_install_idx + 1}: installs a tool/connector in the first plan. "
                f"You CANNOT install in the same plan that asks for permission — the user "
                f"hasn't replied yet. Plan a SINGLE msg task asking whether to install, "
                f"offer alternatives, and end the plan there. The install happens in the "
                f"next cycle after the user approves."
            )

    # after installing a tool that was proposed in a prior turn, the
    # original request is still pending — must replan to continue with it.
    if install_approved:
        has_install_exec = any(
            t.get("type") == TASK_TYPE_EXEC
            and _INSTALL_CMD_RE.search(t.get("detail", ""))
            for t in tasks
        )
        if has_install_exec and tasks[-1].get("type") == TASK_TYPE_MSG:
            errors.append(
                "Plan installs a tool after user approval but ends with msg. "
                "The original request is still pending — use replan as the last "
                "task so the next cycle can fulfill the original request."
            )

    last = tasks[-1]
    if last.get("type") not in (TASK_TYPE_MSG, TASK_TYPE_REPLAN):
        errors.append("Last task must be type 'msg' or 'replan'")

    return errors


# Types that can participate in parallel groups.
_GROUPABLE_TYPES = frozenset({TASK_TYPE_EXEC, TASK_TYPE_SEARCH, TASK_TYPE_TOOL})


def _validate_plan_groups(tasks: list[dict]) -> list[str]:
    """M695: Validate parallel group constraints.

    Rules:
    - group only on exec/search/tool (msg/replan → error)
    - Same-group tasks must be adjacent
    - Each group value must have ≥2 tasks
    """
    errors: list[str] = []
    group_indices: dict[int, list[int]] = {}  # group → [task indices]

    for i, t in enumerate(tasks):
        g = t.get("group")
        if g is None:
            continue
        if t.get("type") not in _GROUPABLE_TYPES:
            errors.append(
                f"Task {i + 1}: group is only allowed on exec/search/tool tasks, "
                f"not '{t.get('type')}'. Remove the group field."
            )
            continue
        group_indices.setdefault(g, []).append(i)

    for g, indices in sorted(group_indices.items()):
        if len(indices) < 2:
            errors.append(
                f"Group {g} has only 1 task (task {indices[0] + 1}). "
                f"Remove the group or add more tasks to the group."
            )
        # Check adjacency: indices must be consecutive integers
        elif indices != list(range(indices[0], indices[0] + len(indices))):
            errors.append(
                f"Group {g} tasks are not adjacent (tasks {', '.join(str(x+1) for x in indices)}). "
                f"Grouped tasks must be consecutive in the plan."
            )

    return errors


def validate_plan(
    plan: dict,
    installed_skills: list[str] | None = None,
    max_tasks: int | None = None,
    installed_skills_info: dict[str, dict] | None = None,
    is_replan: bool = False,
    install_approved: bool = False,
    registry_hint_names: frozenset[str] | None = None,
    force_msg_only: bool = False,
) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid).

    If installed_skills is provided, tool tasks are validated against it.
    If max_tasks is provided, plans with more tasks are rejected.
    If installed_skills_info is provided (name→tool dict), tool args are
    validated against the schema at plan time.
    If is_replan is False, extend_replan is stripped.
    If registry_hint_names is provided, exec tasks with ``kiso tool install``
    are validated: the name must be in the registry (or a git URL).
    If force_msg_only is True, only msg tasks are allowed — all other task
    types are rejected (set after a tool-not-in-registry rejection).
    """
    errors, tasks = _validate_plan_structure(plan, max_tasks, is_replan)
    if errors:
        return errors
    # M950: after a tool was determined to not exist in any registry,
    # force the planner to produce a msg-only plan.
    if force_msg_only:
        non_msg = [t for t in tasks if t.get("type") != TASK_TYPE_MSG]
        if non_msg:
            errors.append(
                "The requested tool does not exist in any registry. "
                "Plan ONLY msg tasks explaining the situation to the user. "
                "Do NOT plan exec, tool, or search tasks."
            )
            return errors
    errors.extend(_validate_plan_tasks(
        tasks, installed_skills, installed_skills_info,
        install_approved=install_approved,
        registry_hint_names=registry_hint_names,
    ))
    errors.extend(_validate_plan_ordering(
        tasks, is_replan, install_approved,
        has_needs_install=bool(plan.get("needs_install")),
        has_knowledge=bool(plan.get("knowledge")),
    ))
    errors.extend(_validate_plan_groups(tasks))

    # goal mentions creating a file/artifact but plan has no exec/tool task
    goal_words = set(plan.get("goal", "").lower().split())
    has_verb = bool(goal_words & _ARTIFACT_VERBS)
    has_noun = bool(goal_words & _ARTIFACT_NOUNS)
    has_action_task = any(
        t.get("type") in (TASK_TYPE_EXEC, TASK_TYPE_TOOL) for t in tasks
    )
    has_needs_install = bool(plan.get("needs_install"))
    if has_verb and has_noun and not has_action_task and not is_replan and not has_needs_install:
        errors.append(
            "Goal mentions creating a file/document but plan has no exec or tool task. "
            "Add an exec task to write the file to the workspace — "
            "auto-publish will generate a download URL automatically."
        )

    # M968: validate knowledge items (if present)
    knowledge = plan.get("knowledge") or []
    for ki, item in enumerate(knowledge, 1):
        if not isinstance(item, str) or len(item.strip()) < _MIN_PROMOTED_FACT_LEN:
            errors.append(
                f"knowledge[{ki}]: must be a string with at least "
                f"{_MIN_PROMOTED_FACT_LEN} characters"
            )

    # M984: needs_install plans are proposal plans — only msg tasks allowed.
    # Execution tasks go in the NEXT plan after the user approves installation.
    if plan.get("needs_install"):
        non_msg = [t["type"] for t in tasks if t.get("type") != TASK_TYPE_MSG]
        if non_msg:
            errors.append(
                f"needs_install is set — only msg tasks are allowed "
                f"(found: {non_msg}). End the plan with a msg asking for approval."
            )
            return errors

    # coherence — tools listed in needs_install must not appear in tool tasks
    needs = plan.get("needs_install") or []
    if needs:
        for i, t in enumerate(tasks, 1):
            if t.get("type") == TASK_TYPE_TOOL and t.get("tool") in needs:
                if install_approved:
                    errors.append(
                        f"Task {i}: tool '{t['tool']}' is not installed yet. "
                        f"Install is approved — plan ONLY an exec task to install "
                        f"{t['tool']} via the kiso CLI, then replan as last task. "
                        f"Tool tasks go in the NEXT plan after install completes."
                    )
                else:
                    errors.append(
                        f"Task {i}: tool '{t['tool']}' is in needs_install (not "
                        f"available) but used as a tool task. Plan a msg asking "
                        f"to install, then end the plan. The tool task goes in a "
                        f"future plan after the user approves and the tool is installed."
                    )

    return errors


_FACT_CHAR_LIMIT = 200


def _format_split_facts(
    facts: list[dict], session: str, is_admin: bool,
) -> tuple[str, str]:
    """Split facts by session, group by category, return (primary_text, other_text).

    Each text is pre-formatted with grouped facts. Empty string if no facts.
    """
    primary, other = _split_facts_by_session(facts, session, is_admin)
    primary_text = "\n".join(_group_facts_by_category(primary)) if primary else ""
    other_text = "\n".join(_group_facts_by_category(other, label_session=True)) if other else ""
    return primary_text, other_text


def _split_facts_by_session(
    facts: list[dict], session: str, is_admin: bool,
) -> tuple[list[dict], list[dict]]:
    """Split facts into primary (current session + global) and other-session lists.

    Non-admin users see all facts as primary (no session filtering).
    """
    if is_admin:
        primary = [f for f in facts if not f.get("session") or f.get("session") == session]
        other   = [f for f in facts if f.get("session") and f.get("session") != session]
    else:
        primary = facts
        other   = []
    return primary, other


def _group_facts_by_category(fact_list: list[dict], label_session: bool = False) -> list[str]:
    """Group facts by category and return formatted section parts."""
    cats: dict[str, list[str]] = {"project": [], "user": [], "tool": [], "general": []}
    for f in fact_list:
        cat = f.get("category", "general")
        if cat not in cats:
            cat = "general"
        content = f['content']
        if len(content) > _FACT_CHAR_LIMIT:
            content = content[:_FACT_CHAR_LIMIT] + "…"
        line = f"- {content}"
        if label_session and f.get("session"):
            line += f" [session:{f['session']}]"
        cats[cat].append(line)
    parts: list[str] = []
    for cat in ("project", "user", "tool", "general"):
        if cats[cat]:
            parts.append(f"### {cat.title()}\n" + "\n".join(cats[cat]))
    return parts


# Capability keywords → tool name they require.  Used by the
# capability-gap heuristic to inject plugin-install guidance when the
# message implies a capability not covered by installed tools.
# Keep minimal — only precise keywords that unambiguously require a tool.
_KISO_CMD_KEYWORDS = frozenset({"tool", "skill", "connector", "env", "instance", "kiso"})
_USER_MGMT_KEYWORDS = frozenset({"user", "admin", "alias"})



async def _gather_planner_context(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    paraphrased_context: str | None = None,
) -> tuple[str, list, list, list, dict, str, str, str]:
    """Gather all raw context pieces for the planner.

    Returns (summary, facts, pending, recent, context_pool,
    sys_env_essential, sys_env_full, install_ctx).
    The context_pool dict is suitable for the briefer.
    """
    is_admin = user_role == "admin"
    context_limit = int(config.settings["context_messages"])

    # batch independent DB queries
    sess, facts, pending, recent = await asyncio.gather(
        get_session(db, session),
        search_facts(db, new_message, session=session, is_admin=is_admin),
        get_pending_items(db, session),
        get_recent_messages(db, session, limit=context_limit),
    )
    summary = sess["summary"] if sess else ""

    # Format facts for context pool
    facts_text = ""
    if facts:
        primary_text, other_text = _format_split_facts(facts, session, is_admin)
        parts: list[str] = []
        if primary_text:
            parts.append(primary_text)
        if other_text:
            parts.append("### From Other Sessions\n" + other_text)
        facts_text = "\n".join(parts)

    pending_text = _format_pending_items(pending)
    recent_text = build_recent_context(recent, kiso_truncate=0)

    sys_env = get_system_env(config)
    sys_env_essential = build_system_env_essential(sys_env, session=session)
    sys_env_full = build_system_env_section(sys_env, session=session)
    install_ctx = build_install_context(sys_env)

    context_pool: dict = {}
    if summary:
        context_pool["summary"] = summary
    if facts_text:
        context_pool["facts"] = facts_text
    if pending_text:
        context_pool["pending"] = pending_text
    if recent_text:
        context_pool["recent_messages"] = recent_text
    if paraphrased_context:
        context_pool["paraphrased"] = paraphrased_context

    # Full system_env for briefer context pool (so briefer can decide
    # whether the planner needs OS/binary details for install tasks).
    context_pool["system_env"] = sys_env_full

    # inject recipes into context pool
    recipes = discover_recipes()
    recipes_text = build_planner_recipe_list(recipes)
    if recipes_text:
        context_pool["recipes"] = recipes_text
        context_pool["_raw_recipes"] = recipes

    # inject available entities for briefer selection, enriched with fact tags
    all_entities = await get_all_entities(db)
    if all_entities:
        # M978: collect fact tags per entity so the briefer knows what each contains
        entity_ids = [e["id"] for e in all_entities]
        placeholders = ",".join("?" * len(entity_ids))
        cur = await db.execute(
            f"SELECT f.entity_id, ft.tag FROM facts f "
            f"JOIN fact_tags ft ON ft.fact_id = f.id "
            f"WHERE f.entity_id IN ({placeholders}) "
            f"GROUP BY f.entity_id, ft.tag",
            entity_ids,
        )
        entity_tags: dict[int, list[str]] = {}
        for row in await cur.fetchall():
            eid = row[0] if isinstance(row, tuple) else row["entity_id"]
            tag = row[1] if isinstance(row, tuple) else row["tag"]
            entity_tags.setdefault(eid, []).append(tag)

        lines = []
        for e in all_entities:
            tags = entity_tags.get(e["id"], [])
            if tags:
                lines.append(f"{e['name']} ({e['kind']}) [{', '.join(sorted(tags))}]")
            else:
                lines.append(f"{e['name']} ({e['kind']})")
        context_pool["available_entities"] = "\n".join(lines)

    return summary, facts, pending, recent, context_pool, sys_env_essential, sys_env_full, install_ctx


async def build_planner_messages(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_tools: str | list[str] | None = None,
    paraphrased_context: str | None = None,
    is_replan: bool = False,
    install_approved: bool = False,
) -> tuple[list[dict], list[str], list[dict]]:
    """Build the message list for the planner LLM call.

    Assembles context from session summary, facts, pending questions,
    system environment, tools, and recent messages.

    When ``briefer_enabled`` is True in config, calls the briefer LLM to
    select prompt modules, filter tools, and synthesize context. Falls
    back to full context on briefer failure.

    Returns (messages, installed_tool_names, installed_tools_info) — the
    caller can reuse the tool names list for plan validation and the
    tools_info list for args validation without rescanning the filesystem.
    """
    # Gather raw context
    summary, facts, pending, recent, context_pool, sys_env_essential, sys_env_full, install_ctx = \
        await _gather_planner_context(
            db, config, session, user_role, new_message, paraphrased_context,
        )

    # system env doesn't change between plan and replan — exclude from
    # briefer context pool to reduce redundant tokens.
    if is_replan:
        context_pool.pop("system_env", None)

    # Session workspace file listing — lets planner see what files exist
    from kiso.worker.utils import _list_session_files, _load_last_plan_summary
    session_files = await asyncio.to_thread(_list_session_files, session)
    if session_files:
        context_pool["session_files"] = session_files

    # Cross-plan state — what the previous plan did (M823)
    last_plan = await asyncio.to_thread(_load_last_plan_summary, session)
    if last_plan:
        context_pool["last_plan"] = last_plan

    # Tool discovery — rescan on each planner call
    installed = discover_tools()
    installed_names = [s["name"] for s in installed]
    if installed_names:
        log.info("discover_tools() found: %s", ", ".join(installed_names))

    # Build the tool list text for context pool
    full_tool_list = build_planner_tool_list(installed, user_role, user_tools)
    if full_tool_list:
        context_pool["tools"] = full_tool_list

    # Connector discovery — show installed connectors to planner
    connectors = discover_connectors()
    if connectors:
        lines = ["Installed connectors:"]
        for c in connectors:
            lines.append(f"- {c['name']} — {c.get('description', '')} ({c.get('platform', '')})")
        context_pool["connectors"] = "\n".join(lines)

    msg_lower = new_message.lower()

    # Uploaded files hint — only when docreader is NOT installed
    if "[uploaded files:" in msg_lower and "docreader" not in installed_names:
        context_pool["upload_hint"] = (
            "The user's message references uploaded files. "
            "Use exec tasks (cat, head, python) to read them from the uploads/ directory."
        )

    _sysenv_registry_hint_names = _parse_registry_hint_names(
        (get_system_env(config) or {}).get("registry_hints", "")
    )

    # --- Registry: show available-but-not-installed tools ---
    # Show uninstalled registry tools so the planner knows what's available
    # for install.  Filtered by installed_names, so returns empty when all
    # tools are installed.  Skip on replans — tools won't change mid-replan.
    registry_text = ""
    if not is_replan:
        registry_text = await asyncio.to_thread(
            get_registry_tools, set(installed_names),
        )
    install_route = _classify_install_mode(
        new_message,
        get_system_env(config),
        installed_tool_names=installed_names,
        registry_hint_names=_sysenv_registry_hint_names,
    )
    install_mode_ctx = _build_install_mode_context(install_route, get_system_env(config))

    # --- Briefer path ---
    briefing = None
    if setting_bool(config.settings, "briefer_enabled"):
        # Fact tags for briefer-driven retrieval (only fetched when briefer active)
        all_tags = await get_all_tags(db)
        if all_tags:
            context_pool["available_tags"] = ", ".join(all_tags)
        try:
            briefing = await run_briefer(
                config, "planner", new_message, context_pool,
                session=session, is_replan=is_replan,
            )
        except Exception as exc:
            log.warning("Briefer failed for planner, falling back to full context: %s", exc)

    # M825: session_files module when files exist in workspace
    _has_session_files = "session_files" in context_pool

    if briefing:
        # Briefer path: modules selected by the briefer LLM.
        # Safety net: force kiso_native (install decision rules) when no tools.
        # Note: plugin_install NOT forced here — it has "curl registry" advice
        # that conflicts with the core "not in hints → apt-get" rule. The
        # briefer selects plugin_install when actually needed.
        modules = list(briefing["modules"])
        if not installed or registry_text:
            if "kiso_native" not in modules:
                modules.append("kiso_native")
        if _has_session_files and "session_files" not in modules:
            modules.append("session_files")
        # M959: planning_rules contains fundamental task-ordering and
        # expect rules that must always be present (matches fallback path).
        if "planning_rules" not in modules:
            modules.append("planning_rules")
        # M1054: tools_rules needed when any tools are installed — contains
        # "use directly" rule and args/guide validation.  Broader than M1049
        # (which checked briefing["tools"]) because the briefer sometimes
        # skips tool selection even when tools are relevant.
        if installed and "tools_rules" not in modules:
            modules.append("tools_rules")
        system_prompt = _load_modular_prompt("planner", modules)
    else:
        # Fallback path: keyword-based module selection (no briefer).
        fallback_modules: list[str] = list(BRIEFER_MODULES - {
            "kiso_commands", "user_mgmt", "plugin_install",
        })
        msg_words = set(msg_lower.split())
        if _KISO_CMD_KEYWORDS & msg_words:
            fallback_modules.append("kiso_commands")
        if _USER_MGMT_KEYWORDS & msg_words:
            fallback_modules.append("user_mgmt")
        _plugin_kw_hit = (
            {"install", "plugin", "add"} & msg_words
            or "not installed" in msg_lower
            or "registry" in msg_lower
        )
        if _plugin_kw_hit or not installed or registry_text:
            fallback_modules.append("plugin_install")
        if _has_session_files:
            fallback_modules.append("session_files")
        system_prompt = _load_modular_prompt("planner", fallback_modules)

    if not installed:
        log.warning("discover_tools() returned empty — no tools available for planner")

    is_admin = user_role == "admin"

    # --- Scored fact retrieval (briefer path only) ---
    scored_facts_text = ""
    if briefing:
        entity_id = None
        if briefing.get("relevant_entities"):
            all_entities = await get_all_entities(db)
            entity_map = {_normalize_entity_name(e["name"]): e["id"] for e in all_entities}
            # filter out hallucinated entity names
            valid_entities = []
            for ename in briefing["relevant_entities"]:
                eid = entity_map.get(_normalize_entity_name(ename))
                if eid is not None:
                    valid_entities.append(ename)
                    if entity_id is None:
                        entity_id = eid  # primary entity
            briefing["relevant_entities"] = valid_entities
        scored_facts = await search_facts_scored(
            db,
            entity_id=entity_id,
            tags=briefing.get("relevant_tags") or None,
            keywords=new_message.lower().split()[:10] if new_message else None,
            session=session if not is_admin else None,
            is_admin=is_admin,
        )
        if scored_facts:
            scored_facts_text = "\n".join(f"- {f['content']}" for f in scored_facts)

    # --- Build context block ---
    context_parts: list[str] = []

    if briefing:
        # Briefer path: use synthesized context + filtered skills
        _add_section(context_parts, "Context", briefing["context"])
        _add_section(context_parts, "Relevant Facts", scored_facts_text)
        # M937: inject essential system env always (~60 tok). Full version
        # (~400 tok) only when briefer selected install/system modules.
        # Check briefer's raw selection — force-added modules (kiso_native
        # safety net) don't count since they're added unconditionally.
        _SYSENV_MODULES = {"plugin_install", "kiso_commands", "user_mgmt"}
        _needs_full_sysenv = bool(set(briefing["modules"]) & _SYSENV_MODULES)
        if _needs_full_sysenv:
            context_parts.append(f"## System Environment\n{sys_env_full}")
        else:
            context_parts.append(f"## System Environment\n{sys_env_essential}")
            # M963: when kiso_native is loaded (install-decision rules) but
            # full sysenv isn't warranted, inject just the install-critical
            # fields so the planner can route install commands correctly.
            if "kiso_native" in modules and install_ctx:
                _add_section(context_parts, "Install Context", install_ctx)
        _add_section(context_parts, "Install Routing", install_mode_ctx)
        # M1040: inject user-facing settings only when kiso_commands loaded.
        if "kiso_commands" in modules:
            _settings_text = build_user_settings_text(get_system_env(config))
            _add_section(context_parts, "User Settings", _settings_text)
        # Session workspace files + previous plan results — operational data
        # that must reach the planner verbatim (not gated by briefer synthesis).
        _add_section(context_parts, "Session Workspace", context_pool.get("session_files", ""))
        _add_section(context_parts, "Previous Plan", context_pool.get("last_plan", ""))
    else:
        # Fallback path: full context dump (original behavior)
        _add_section(context_parts, "Session Summary", summary)

        if facts:
            primary_text, other_text = _format_split_facts(facts, session, is_admin)
            if primary_text:
                context_parts.append("## Known Facts\n" + primary_text)
            if other_text:
                context_parts.append("## Context from Other Sessions\n" + other_text)

        # entity-based fact enrichment (parity with briefer path)
        # Use word-level matching with normalization so "config" matches entity "configuration"
        all_entities = await get_all_entities(db)
        if all_entities:
            msg_words = set(new_message.lower().split())
            existing_ids = {f["id"] for f in facts} if facts else set()
            for ent in all_entities:
                ent_norm = _normalize_entity_name(ent["name"])
                ent_words = set(ent_norm.split())
                if ent_words & msg_words or ent_norm in new_message.lower():
                    ent_facts = await search_facts_by_entity(db, ent["id"])
                    new_facts = [f for f in ent_facts if f["id"] not in existing_ids]
                    if new_facts:
                        extra = "\n".join(f"- {f['content']}" for f in new_facts)
                        context_parts.append(f"## Additional Facts (entity: {ent['name']})\n{extra}")
                        existing_ids.update(f["id"] for f in new_facts)

        # System env in original position (after facts, before pending)
        # Fallback path: inject full system env (conservative, no briefer).
        context_parts.append(f"## System Environment\n{sys_env_full}")
        _add_section(context_parts, "Install Routing", install_mode_ctx)
        # Session workspace files + previous plan results (same as briefer path)
        _add_section(context_parts, "Session Workspace", context_pool.get("session_files", ""))
        _add_section(context_parts, "Previous Plan", context_pool.get("last_plan", ""))

        _add_section(context_parts, "Pending Questions", _format_pending_items(pending))

        if recent:
            context_parts.append(
                f"## Recent Messages\n{fence_content(build_recent_context(recent, kiso_truncate=0), 'MESSAGES')}"
            )

        if paraphrased_context:
            context_parts.append(
                f"## Paraphrased External Messages (untrusted)\n"
                f"{fence_content(paraphrased_context, 'PARAPHRASED')}"
            )

    # Recipes section — exclusion model: inject all minus briefer-excluded.
    if context_pool.get("recipes"):
        raw_recipes = context_pool.get("_raw_recipes", [])
        if briefing:
            excluded = {n.lower() for n in briefing.get("exclude_recipes", [])}
            kept = [r for r in raw_recipes if r["name"].lower() not in excluded]
        else:
            kept = raw_recipes
        if kept:
            context_parts.append(f"## Available Recipes\n{build_planner_recipe_list(kept)}")

    # Tools section — briefer selects by name, code injects full descriptions.
    # M824: skip briefer tool filtering when few tools installed — marginal
    # token saving vs catastrophic risk of excluding the right tool.
    tool_filter_threshold = setting_int(
        config.settings, "briefer_tool_filter_threshold", lo=0,
    )
    if briefing and briefing["tools"]:
        if len(installed) <= tool_filter_threshold:
            # Few tools — inject all but with guides only for selected tools
            log.debug("Skipping briefer tool filter: %d tools <= threshold %d",
                      len(installed), tool_filter_threshold)
            _selected = set(briefing["tools"])
            tiered_list = build_planner_tool_list(installed, user_role, user_tools, selected_names=_selected)
            if tiered_list:
                context_parts.append(f"## Tools\n{tiered_list}")
        else:
            selected_names = set(briefing["tools"])
            selected_tools = [t for t in installed if t["name"] in selected_names]
            selected_tool_text = build_planner_tool_list(selected_tools, user_role, user_tools)
            if selected_tool_text:
                context_parts.append(f"## Tools\n{selected_tool_text}")
    elif full_tool_list:
        context_parts.append(f"## Tools\n{full_tool_list}")

    # warn planner when web module is active but browser isn't installed.
    # Emphasise that built-in search works without any tool for research queries.
    if "web" in (modules if briefing else fallback_modules) and "browser" not in installed_names:
        context_parts.append(
            "## Browser Availability\n"
            "The browser tool is NOT installed. "
            "For web research and reading page content, use the built-in `search` task type — "
            "it requires no tool and works immediately. "
            "The browser tool is only needed for interactive browsing (navigate to a specific URL, "
            "click, fill forms, take screenshots). "
            "If interactive browsing is required: single msg asking to install, end plan.\n"
            "Note: if the user also asks to create/write a file, an exec task is still "
            "required — search alone cannot create files."
        )

    # always-inject available registry tools (not gated by briefer) so the
    # planner knows what tools can be installed via `kiso tool install`.
    if registry_text:
        context_parts.append(f"## Available Tools (not installed)\n{registry_text}")

    # M954: clarify that built-in search works without websearch installation.
    # This is injected unconditionally (not gated by briefer web module) so the
    # planner never fixates on installing websearch for simple research queries.
    # registry_text only contains websearch when it's NOT installed.
    if registry_text and "websearch" in registry_text:
        context_parts.append(
            "**Note:** The built-in `search` task type handles all web research "
            "queries without any tool installation. Use `type: search` directly. "
            "For file creation requests, combine search with exec tasks."
        )

    # always-inject safety facts (not gated by briefer)
    safety_facts = await get_safety_facts(db)
    _add_section(context_parts, "Safety Rules (MUST OBEY)",
                 _join_or_empty(safety_facts, lambda f: f"- {f['content']}"))

    # always-inject behavior facts (soft guidelines, not hard constraints)
    behavior_facts = await get_behavior_facts(db)
    _add_section(context_parts, "Behavior Guidelines (follow these preferences)",
                 _join_or_empty(behavior_facts, lambda f: f"- {f['content']}"))

    # tell the planner it may proceed with install execs when approved.
    if install_approved:
        context_parts.append(
            "## Install Status\n"
            "A prior plan proposed tool installation and the user approved. "
            "Do NOT set needs_install — the user has already approved. "
            "Plan exec tasks to install directly via the kiso CLI "
            "(e.g., exec 'kiso tool install browser'), then replan as last task. "
            "Do NOT add tool tasks for uninstalled tools — they become "
            "available after the replan. "
            "For tools already installed: use them directly."
        )

    context_parts.append(f"## Caller Role\n{user_role}")
    context_parts.append(f"## New Message\n{fence_content(new_message, 'USER_MSG')}")

    context_block = "\n\n".join(context_parts)

    return _build_messages(system_prompt, context_block), installed_names, installed


async def run_planner(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_tools: str | list[str] | None = None,
    paraphrased_context: str | None = None,
    on_context_ready: Callable | None = None,
    on_retry: Callable[[int, int, str], None] | None = None,
    is_replan: bool = False,
    install_approved: bool = False,
    max_tasks_override: int | None = None,
) -> dict:
    """Run the planner: build context, call LLM, validate, retry if needed.

    Args:
        on_context_ready: Optional async callback invoked after the briefer
            completes but before the planner LLM call.  The caller can use
            this to flush intermediate usage so the CLI can render briefer
            panels while the planner is still running.
        on_retry: Optional callback(attempt, max_attempts, reason) called
            before each retry attempt.
        max_tasks_override: override max_plan_tasks (used by replan shrinking
            to reduce the limit at deeper replan depths).

    Returns the validated plan dict with keys: goal, secrets, tasks.
    Raises PlanError if all retries exhausted.
    """
    messages, installed_names, installed_info = await build_planner_messages(
        db, config, session, user_role, new_message, user_tools=user_tools,
        paraphrased_context=paraphrased_context, is_replan=is_replan,
        install_approved=install_approved,
    )
    if on_context_ready:
        await on_context_ready()
    tools_by_name = {s["name"]: s for s in installed_info}

    max_tasks = max_tasks_override if max_tasks_override is not None else int(config.settings["max_plan_tasks"])

    # Extract registry hint names for install validation (M862).
    _sysenv = get_system_env(config)
    _reg_hint_names = _parse_registry_hint_names(_sysenv.get("registry_hints", ""))

    # inject task budget into planner context so LLM knows the limit.
    budget_line = f"\n\n## Task Budget\nMaximum tasks: {max_tasks}."
    for msg in reversed(messages):
        if msg["role"] == "user":
            msg["content"] += budget_line
            break
    else:
        log.warning("No user message found for budget injection")

    # M950: track whether a tool was rejected as not-in-registry across
    # validation retries.  Once triggered, subsequent retries must produce
    # a msg-only plan (prevents the planner from circumventing via exec).
    _force_msg = False

    def _validate_plan(p: dict) -> list[str]:
        nonlocal _force_msg
        errs = validate_plan(
            p, installed_skills=installed_names, max_tasks=max_tasks,
            installed_skills_info=tools_by_name, is_replan=is_replan,
            install_approved=install_approved,
            registry_hint_names=_reg_hint_names,
            force_msg_only=_force_msg,
        )
        if any(_TOOL_UNAVAILABLE_MARKER in e for e in errs):
            _force_msg = True
        return errs

    fallback = config.settings.get("planner_fallback_model") or None
    plan = await _retry_llm_with_validation(
        config, "planner", messages, PLAN_SCHEMA,
        _validate_plan,
        PlanError, "Plan",
        session=session,
        on_retry=on_retry,
        fallback_model=fallback,
    )
    # detect install proposal from three sources:
    # 1. Planner explicitly declared needs_install (preferred, direct)
    # 2. Validation saw uninstalled-tool errors (backup, indirect)
    # 3. No tools installed AND plan has no tool tasks — on a fresh instance,
    #    any valid plan (msg-only, search+msg, exec+msg) is implicitly an
    #    install-proposal context. Tool tasks can't exist in valid plans on
    #    fresh instances (validation rejects them), so this condition is True
    #    for all valid plans when no tools are installed.
    #    False positives are harmless: install_approved only *enables* install
    #    execs in the next turn, it doesn't force them.
    saw_uninstalled = plan.pop("_saw_uninstalled_tool", False)
    tasks = plan.get("tasks") or []

    # Filter needs_install: remove tools that are already installed.
    # The LLM sometimes lists installed tools in needs_install by mistake.
    needs = plan.get("needs_install") or []
    if needs and installed_names:
        needs = [n for n in needs if n not in installed_names]
        plan["needs_install"] = needs or None

    plan["install_proposal"] = (
        bool(plan.get("needs_install"))
        or saw_uninstalled
        or (not installed_names
            and not any(t.get("type") == TASK_TYPE_TOOL for t in tasks))
    )

    log.info("Plan: goal=%r, %d tasks, install_proposal=%s",
             plan["goal"], len(plan["tasks"]), plan["install_proposal"])
    return plan


# ---------------------------------------------------------------------------
# Briefer (context intelligence layer)
# ---------------------------------------------------------------------------


_CONTEXT_POOL_SECTIONS: tuple[tuple[str, str], ...] = (
    ("tools", "Available Tools"),
    ("recipes", "Available Recipes"),
    ("connectors", "Available Connectors"),
    ("system_env", "System Environment"),
    ("summary", "Session Summary"),
    ("facts", "Known Facts"),
    ("recent_messages", "Recent Messages"),
    ("pending", "Pending Questions"),
    ("available_tags", "Available Fact Tags"),
    ("available_entities", "Available Entities"),
    ("paraphrased", "Paraphrased External Messages"),
    ("session_files", "Session Workspace"),
    ("last_plan", "Previous Plan"),
    ("replan_context", "Replan Context"),
    ("plan_outputs", "Plan Outputs"),
)


def _prefilter_context_pool(
    context_pool: dict, consumer_role: str, is_replan: bool = False,
) -> dict:
    """Remove context pool sections unlikely to be relevant.

    Returns a shallow copy with irrelevant keys removed.
    """
    pool = dict(context_pool)
    # replan_context only useful during replanning
    if not is_replan:
        pool.pop("replan_context", None)
    # plan_outputs: needed by messenger (formats response) and replan,
    # but not by planner on first plan
    if not is_replan and consumer_role == "planner":
        pool.pop("plan_outputs", None)
    # recipes only relevant when recipes are installed
    if not pool.get("recipes"):
        pool.pop("recipes", None)
    # _raw_recipes is internal (list of dicts), not for LLM consumption
    pool.pop("_raw_recipes", None)
    return pool


def build_briefer_messages(
    consumer_role: str,
    task_description: str,
    context_pool: dict,
    is_replan: bool = False,
) -> list[dict]:
    """Build the message list for the briefer LLM call.

    Args:
        consumer_role: Which role the briefing is for (e.g. "planner", "messenger").
        task_description: What the consumer needs to accomplish.
        context_pool: Dict of available context pieces. Keys match
            ``_CONTEXT_POOL_SECTIONS`` (all optional).
        is_replan: Whether this is a replan iteration (keeps replan_context/plan_outputs).
    """
    pool = _prefilter_context_pool(context_pool, consumer_role, is_replan)
    system_prompt = _load_system_prompt("briefer")

    # messenger/worker never use modules or skills — omit those sections
    # to save ~400 tokens per briefer call for these simple consumers.
    _simple_consumer = consumer_role in ("messenger", "worker")

    parts: list[str] = [
        f"## Consumer Role\n{consumer_role}",
        f"## Task\n{task_description}",
    ]
    if not _simple_consumer:
        parts.append(f"## Available Modules\n{_BRIEFER_MODULES_STR}")

    # skip sections irrelevant for simple consumers
    _skip_keys = {"tools", "system_env", "connectors"} if _simple_consumer else set()

    for key, heading in _CONTEXT_POOL_SECTIONS:
        if key in _skip_keys:
            continue
        if val := pool.get(key):
            parts.append(f"## {heading}\n{val}")

    return _build_messages(system_prompt, "\n\n".join(parts))


def validate_briefing(briefing: dict, *, check_modules: bool = True) -> list[str]:
    """Validate briefing semantics. Returns list of error strings.

    When *check_modules* is False, module names are not checked against
    ``BRIEFER_MODULES``.  Used for simple consumers (messenger, worker)
    that never use modules — avoids wasted retries when the model
    hallucinates module names it was never shown.
    """
    errors: list[str] = []
    if not isinstance(briefing.get("modules"), list):
        errors.append("modules must be an array")
    elif check_modules:
        for m in briefing["modules"]:
            if m not in BRIEFER_MODULES:
                errors.append(f"unknown module: {m!r}")
    if not isinstance(briefing.get("tools"), list):
        errors.append("tools must be an array")
    if not isinstance(briefing.get("exclude_recipes"), list):
        errors.append("exclude_recipes must be an array")
    if not isinstance(briefing.get("context"), str):
        errors.append("context must be a string")
    if not isinstance(briefing.get("output_indices"), list):
        errors.append("output_indices must be an array")
    if not isinstance(briefing.get("relevant_tags"), list):
        errors.append("relevant_tags must be an array")
    if not isinstance(briefing.get("relevant_entities"), list):
        errors.append("relevant_entities must be an array")
    return errors


_POOL_NAME_RE = re.compile(r"^-\s+(\S+)")


def _filter_briefer_names(
    names: list[str], pool_text: str | None, label: str,
) -> list[str]:
    """Filter hallucinated briefer names against available names in *pool_text*.

    *pool_text* is the formatted context_pool section (e.g. tools or recipes).
    Names are extracted from lines matching ``- name ...``.
    Returns the filtered list (normalized to lowercase).
    """
    if not names:
        return []
    if not pool_text:
        log.debug("Briefer: cleared %d hallucinated %s(s) (none available)",
                   len(names), label)
        return []
    available: set[str] = set()
    for line in pool_text.split("\n"):
        m = _POOL_NAME_RE.match(line)
        if m:
            available.add(m.group(1).lower())
    filtered = [n.strip().lower() for n in names if n.strip().lower() in available]
    removed = len(names) - len(filtered)
    if removed:
        log.debug("Briefer: filtered %d hallucinated %s name(s)", removed, label)
    return filtered


async def run_briefer(
    config: Config,
    consumer_role: str,
    task_description: str,
    context_pool: dict,
    session: str = "",
    is_replan: bool = False,
) -> dict:
    """Run the briefer: select relevant context for a consumer role.

    Returns a dict with keys: modules, tools, context, output_indices, relevant_tags.
    Raises BrieferError on failure.
    """
    messages = build_briefer_messages(
        consumer_role, task_description, context_pool, is_replan=is_replan,
    )

    # simple consumers never use modules — skip module name validation
    # to avoid wasted retries when the model hallucinates names.
    _simple = consumer_role in ("messenger", "worker")
    vfn = (lambda b: validate_briefing(b, check_modules=False)) if _simple else validate_briefing

    briefing = await _retry_llm_with_validation(
        config, "briefer", messages, BRIEFER_SCHEMA,
        vfn,
        BrieferError, "Briefing",
        session=session,
    )

    # force modules/exclude_recipes=[] for simple consumers (defensive cleanup)
    if _simple:
        briefing["modules"] = []
        briefing["exclude_recipes"] = []

    # post-validation filtering — remove hallucinated names
    briefing["tools"] = _filter_briefer_names(
        briefing.get("tools", []), context_pool.get("tools"), "tool")
    briefing["exclude_recipes"] = _filter_briefer_names(
        briefing.get("exclude_recipes", []), context_pool.get("recipes"), "recipe")

    log.info(
        "Briefer for %s: %d modules, %d tools, %d exclude_recipes, %d output_indices, %d tags",
        consumer_role,
        len(briefing["modules"]),
        len(briefing["tools"]),
        len(briefing.get("exclude_recipes", [])),
        len(briefing["output_indices"]),
        len(briefing.get("relevant_tags", [])),
    )
    return briefing


# ---------------------------------------------------------------------------
# Classifier (fast path)
# ---------------------------------------------------------------------------


class ClassifierError(Exception):
    """Classifier generation failure."""


def build_classifier_messages(
    content: str, recent_context: str = "",
    entity_names: str = "",
) -> list[dict]:
    """Build the message list for the classifier LLM call."""
    user_text = content
    if recent_context:
        user_text = f"{content}\n\n## Recent Conversation\n{recent_context}"
    if entity_names:
        user_text = f"{user_text}\n\n## Known Entities\n{entity_names}"
    return _build_messages(_load_system_prompt("classifier"), user_text)


CLASSIFIER_CATEGORIES: frozenset[str] = frozenset({"plan", "chat", "chat_kb"})


async def classify_message(
    config: Config, content: str, session: str = "",
    recent_context: str = "",
    entity_names: str = "",
) -> tuple[str, str]:
    """Classify a user message and detect its language.

    Returns ``(category, lang)`` where *category* is one of
    :data:`CLASSIFIER_CATEGORIES` and *lang* is a full English
    language name (e.g. ``"English"``, ``"Italian"``).  On any error
    or ambiguous output, returns ``("plan", "")`` as safe fallback.
    """
    messages = build_classifier_messages(
        content, recent_context=recent_context, entity_names=entity_names,
    )
    try:
        raw = await call_llm(config, "classifier", messages, session=session)
    except LLMError as e:
        log.warning("Classifier LLM failed, falling back to plan: %s", e)
        return "plan", ""

    log.info("Classifier raw LLM output: %r", raw.strip())
    result = raw.strip()

    # Expected format: "cat:Language" (e.g. "chat:Italian", "plan:English")
    if ":" in result:
        cat, lang = result.split(":", 1)
        cat = cat.strip().lower()
        lang = lang.strip().title()  # "russian" → "Russian"
        if cat in CLASSIFIER_CATEGORIES and lang:
            log.info("Classifier: %s (lang=%s)", cat, lang)
            return cat, lang
        # Defensive: LLM returned literal "category:Language" or
        # "category:Language:cat"
        if cat == "category" and ":" in lang:
            lang_part, cat_part = lang.split(":", 1)
            lang_part = lang_part.strip().title()
            cat_part = cat_part.strip().lower()
            if cat_part in CLASSIFIER_CATEGORIES and lang_part:
                log.info("Classifier: %s (literal 'category', lang=%s)", cat_part, lang_part)
                return cat_part, lang_part
        if cat == "category" and lang:
            log.info("Classifier: plan (literal 'category', lang=%s)", lang)
            return "plan", lang

    # LLM fallback: plain category without lang — don't force a language,
    # let the messenger detect language from the user message.
    if result.lower() in CLASSIFIER_CATEGORIES:
        log.info("Classifier: %s (no lang — messenger will detect)", result.lower())
        return result.lower(), ""

    # Ambiguous output — safe fallback (plan, no forced language)
    log.warning("Classifier returned unexpected value %r, falling back to plan", raw.strip())
    return "plan", ""


# --- Stop pattern fast-path ---

_STOP_PATTERNS = re.compile(
    r"^(stop|ferma|fermati|annulla|cancel|abort|basta|quit)[\s!.]*$",
    re.IGNORECASE,
)
_URGENT_RE = re.compile(r"^[A-Z\s!]{4,}$")


def is_stop_message(text: str) -> bool:
    """Return True if *text* is an obvious stop/cancel command.

    Matches single stop words (with optional trailing punctuation) and
    ALL-CAPS urgent messages (≥4 chars).  Does NOT match messages with
    content after the stop word (e.g. "stop using port 80").
    """
    s = text.strip()
    if _STOP_PATTERNS.match(s):
        return True
    if _URGENT_RE.match(s):
        return True
    return False


# --- In-flight message classification ---


def build_inflight_classifier_messages(
    plan_goal: str, new_message: str, recent_context: str = "",
) -> list[dict]:
    """Build the message list for in-flight message classification."""
    template = _load_system_prompt("inflight-classifier")
    # Inject recent conversation if available
    conv_block = f"Recent conversation:\n{recent_context}\n\n" if recent_context else ""
    user_text = template.replace(
        "{plan_goal}", plan_goal,
    ).replace(
        "{new_message}", new_message,
    ).replace(
        "{recent_conversation}", conv_block,
    )
    return [{"role": "user", "content": user_text}]


async def classify_inflight(
    config: Config, plan_goal: str, new_message: str,
    session: str = "", recent_context: str = "",
) -> str:
    """Classify an in-flight message as stop/update/independent/conflict.

    Returns one of :data:`INFLIGHT_CATEGORIES`. On any error, returns
    ``"independent"`` (safe fallback — message will be queued for later).
    """
    messages = build_inflight_classifier_messages(plan_goal, new_message, recent_context)
    try:
        raw = await call_llm(config, "classifier", messages, session=session)
    except LLMError as e:
        log.warning("Inflight classifier LLM failed, falling back to independent: %s", e)
        return "independent"

    result = raw.strip().lower()
    if result in INFLIGHT_CATEGORIES:
        log.info("Inflight classifier: %s", result)
        return result

    log.warning("Inflight classifier returned unexpected value %r, falling back to independent",
                raw.strip())
    return "independent"


def validate_review(review: dict) -> list[str]:
    """Validate review semantics. Returns list of error strings."""
    errors: list[str] = []
    status = review.get("status")
    if status not in REVIEW_STATUSES:
        errors.append(f"status must be 'ok', 'replan', or 'stuck', got {status!r}")
        return errors
    if status in (REVIEW_STATUS_REPLAN, REVIEW_STATUS_STUCK) and not review.get("reason"):
        errors.append(f"{status} status requires a non-null, non-empty reason")
    return errors


# --- Learning quality filters ---

_EPHEMERAL_LEARN_RE = re.compile(
    r"\[\d+\].*\[\d+\]"  # 2+ browser element indices like [8], [9]
)
_TRANSIENT_LEARN_RE = re.compile(
    r"\b(installed|loaded|ran|started|completed|finished)\s+successfully\b",
    re.IGNORECASE,
)
_MIN_LEARN_LEN = 15
_NEG_CLAIM_PATTERNS = (
    "not found", "not available", "not stated",
    "does not support", "not installed",
)


def _learning_contradicts_output(learning: str, output: str) -> bool:
    """Check if a negative-claim learning is contradicted by the task output.

    Returns True when the learning says something is "not found" / "not available"
    but the subject term actually appears in the output.
    """
    learning_lower = learning.lower()
    output_lower = output.lower()
    for neg in _NEG_CLAIM_PATTERNS:
        if neg in learning_lower:
            idx = learning_lower.index(neg)
            subject_words = learning[:idx].strip().split()[-2:]
            if any(
                w.lower() in output_lower
                for w in subject_words
                if len(w) > 3
            ):
                return True
    return False


def clean_learn_items(
    items: list[str], task_output: str | None = None,
) -> list[str]:
    """Filter out low-quality learn items from a reviewer response.

    Removes items that are:
    - Too short (< 15 chars) — fragmentary
    - Contain 2+ browser element indices ``[N]`` — ephemeral session data
    - Match transient patterns like "X installed successfully"
    - Negative claims contradicted by task output
    """
    kept: list[str] = []
    for item in items:
        if len(item) < _MIN_LEARN_LEN:
            log.debug("Learn item filtered (too short): %s", item)
            continue
        if _EPHEMERAL_LEARN_RE.search(item):
            log.debug("Learn item filtered (ephemeral indices): %s", item[:80])
            continue
        if _TRANSIENT_LEARN_RE.search(item):
            log.debug("Learn item filtered (transient): %s", item[:80])
            continue
        if task_output and _learning_contradicts_output(item, task_output):
            log.debug("Learn item filtered (contradicts output): %s", item[:80])
            continue
        kept.append(item)
    return kept


_EXIT_CODE_NOTES: dict[int, str] = {
    1: "Note: exit 1 from grep/which/find/dpkg means 'no matches found', not an error.",
    2: "Note: exit 2 often indicates a usage/syntax error in the command.",
    126: "Note: exit 126 means the command was found but is not executable (permission issue).",
    127: "Note: exit 127 means the command was not found in PATH.",
    -1: "Note: the process was killed (OS error).",
}


_REVIEWER_OUTPUT_LIMIT = 16_000
_REVIEWER_STDERR_BUDGET = 2000
_REVIEWER_STDERR_MAX_LINES = 40
_REVIEWER_MAX_GREP_MATCHES = 20

_ERROR_RE = re.compile(
    r"error|fail|exception|traceback|warning|denied|not found|fatal|panic|refused|timeout|errno",
    re.IGNORECASE,
)

def _sanitize_for_reviewer(text: str) -> str:
    """Strip binary/non-printable content from exec output before sending to reviewer.

    Removes individual non-printable characters (all except printable chars and
    normal whitespace \\t \\n \\r).  Appends a marker if any chars were removed,
    so the reviewer knows output was sanitized.  Normal text and valid Unicode
    are kept unchanged.
    """
    if not text:
        return text
    # Fast path: no suspicious chars
    if "\x00" not in text and "\ufffd" not in text and all(
        c.isprintable() or c in "\t\n\r" for c in text
    ):
        return text

    clean: list[str] = []
    removed = 0
    for c in text:
        # \ufffd is the Unicode replacement char — appears when binary is
        # force-decoded as UTF-8; treat it as non-printable for our purposes.
        if (c.isprintable() or c in "\t\n\r") and c != "\ufffd":
            clean.append(c)
        else:
            removed += 1

    result = "".join(clean)
    if removed:
        result += f"\n[binary content suppressed — {removed} bytes]"
    return result


def prepare_reviewer_output(
    stdout: str, stderr: str, limit: int = _REVIEWER_OUTPUT_LIMIT,
) -> str:
    """Prepare task output for the reviewer LLM.

    For small outputs (≤ *limit*), returns the combined text as-is.
    For large outputs, builds: error section (stderr + error grep) +
    main output (head + truncation marker + tail), all within *limit*.
    """
    stdout = _sanitize_for_reviewer(stdout)
    stderr = _sanitize_for_reviewer(stderr)
    combined = stdout
    if stderr:
        combined += f"\n--- stderr ---\n{stderr}"
    if len(combined) <= limit:
        return combined

    # --- Error section (always present if errors exist) ---
    error_parts: list[str] = []

    # Stderr
    if stderr.strip():
        stderr_lines = stderr.splitlines()[:_REVIEWER_STDERR_MAX_LINES]
        stderr_text = "\n".join(stderr_lines)
        if len(stderr_text) > _REVIEWER_STDERR_BUDGET:
            stderr_text = stderr_text[:_REVIEWER_STDERR_BUDGET] + "\n... (stderr truncated)"
        error_parts.append(f"--- stderr ({len(stderr_lines)} lines) ---\n{stderr_text}")

    # Error grep — scan stdout for error keywords
    stdout_lines = stdout.splitlines()
    grep_matches: list[str] = []
    for i, line in enumerate(stdout_lines):
        if _ERROR_RE.search(line):
            context_line = stdout_lines[i - 1] if i > 0 else ""
            entry = f"{context_line}\n{line}".strip() if context_line else line
            if entry not in grep_matches:
                grep_matches.append(entry)
            if len(grep_matches) >= _REVIEWER_MAX_GREP_MATCHES:
                break

    if grep_matches:
        grep_text = "\n".join(grep_matches)
        error_parts.append(f"--- error matches ---\n{grep_text}")

    error_section = "\n".join(error_parts)

    # --- Main output (head + truncation marker + tail) ---
    # Reserve space for error section + separator
    error_overhead = len(error_section) + 5 if error_section else 0  # "\n---\n"
    main_budget = limit - error_overhead

    if len(stdout) <= main_budget:
        main_section = stdout
    else:
        # Reserve space for marker + newlines (~50 chars)
        usable = main_budget - 50
        half = max(usable // 2, 100)
        skipped = len(stdout) - half * 2
        main_section = (
            f"{stdout[:half]}\n\n"
            f"[... {skipped} chars truncated ...]\n\n"
            f"{stdout[-half:]}"
        )

    # --- Combine ---
    if error_section:
        result = f"{error_section}\n---\n{main_section}"
    else:
        result = main_section

    # Hard cap (should rarely hit after budget calculation)
    if len(result) > limit:
        result = result[:limit]

    return result


def _select_reviewer_modules(
    output: str, safety_rules: list[str] | None,
) -> list[str]:
    """Heuristic module selection for reviewer (no briefer call needed).

    Always includes ``rules``.  Adds ``learn_quality`` when output is
    non-trivial, and ``compliance`` when safety rules are present.
    """
    modules: list[str] = ["rules"]
    if output and len(output.strip()) > 20:
        modules.append("learn_quality")
    if safety_rules:
        modules.append("compliance")
    return modules


def build_reviewer_messages(
    goal: str,
    detail: str,
    expect: str,
    output: str,
    user_message: str,
    success: bool | None = None,
    exit_code: int | None = None,
    safety_rules: list[str] | None = None,
) -> list[dict]:
    """Build the message list for the reviewer LLM call."""
    modules = _select_reviewer_modules(output, safety_rules)
    system_prompt = _load_modular_prompt("reviewer", modules)

    context = (
        f"## Plan Context\n{goal}\n\n"
        f"## Task Detail\n{detail}\n\n"
        f"## Expected Outcome\n{expect}\n\n"
        f"## Actual Output\n{fence_content(output, 'TASK_OUTPUT')}\n\n"
        f"## Original User Message\n{fence_content(user_message, 'USER_MSG')}"
    )

    if success is not None:
        if exit_code is not None:
            if success:
                status_text = f"Exit code: 0 (success)"
            else:
                note = _EXIT_CODE_NOTES.get(exit_code, "")
                status_text = f"Exit code: {exit_code} (non-zero)"
                if note:
                    status_text += f"\n{note}"
        else:
            status_text = "succeeded (exit code 0)" if success else "FAILED (non-zero exit code)"
        context += f"\n\n## Command Status\n{status_text}"

    # inject safety rules for compliance check
    rules_text = _join_or_empty(safety_rules)
    if rules_text:
        context += f"\n\n## Safety Rules (violations → stuck)\n{rules_text}"

    return _build_messages(system_prompt, context)


async def run_reviewer(
    config: Config,
    goal: str,
    detail: str,
    expect: str,
    output: str,
    user_message: str,
    session: str = "",
    success: bool | None = None,
    exit_code: int | None = None,
    safety_rules: list[str] | None = None,
) -> dict:
    """Run the reviewer on a task output.

    Returns dict with keys: status ("ok" | "replan"), reason, learn.
    Raises ReviewError if all retries exhausted.
    """
    messages = build_reviewer_messages(
        goal, detail, expect, output, user_message,
        success=success, exit_code=exit_code,
        safety_rules=safety_rules,
    )
    review = await _retry_llm_with_validation(
        config, "reviewer", messages, REVIEW_SCHEMA,
        validate_review, ReviewError, "Review",
        session=session,
    )
    log.info("Review: status=%s reason=%s", review["status"],
             (review.get("reason") or "")[:200])
    return review


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------

CURATOR_SCHEMA: dict = _build_strict_schema("curator", {
    "evaluations": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "learning_id": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["promote", "ask", "discard"]},
            "fact": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "category": {"anyOf": [{"type": "string", "enum": ["project", "user", "tool", "general", "behavior"]}, {"type": "null"}]},
            "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "reason": {"type": "string"},
            "tags": {"anyOf": [
                {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                {"type": "null"},
            ]},
            "entity_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "entity_kind": {"anyOf": [
                {"type": "string", "enum": sorted(_ENTITY_KINDS)},
                {"type": "null"},
            ]},
        },
        "required": ["learning_id", "verdict", "fact", "category", "question", "reason", "tags", "entity_name", "entity_kind"],
        "additionalProperties": False,
    }},
}, ["evaluations"])


class CuratorError(Exception):
    """Curator validation or generation failure."""


class SummarizerError(Exception):
    """Summarizer generation failure."""


_MIN_PROMOTED_FACT_LEN = 10


def validate_curator(result: dict, expected_count: int | None = None) -> list[str]:
    """Validate curator result semantics. Returns list of error strings.

    *expected_count* is the number of input learnings. The curator may return
    **fewer** evaluations (consolidation) but never **more** than expected.
    """
    errors: list[str] = []
    evals = result.get("evaluations", [])
    if expected_count is not None and len(evals) > expected_count:
        errors.append(f"Expected at most {expected_count} evaluations, got {len(evals)}")
    if expected_count is not None and expected_count > 0 and len(evals) == 0:
        errors.append("Expected at least 1 evaluation but got 0 — every learning must be evaluated")
    for i, ev in enumerate(evals, 1):
        verdict = ev.get("verdict")
        if not ev.get("reason"):
            errors.append(f"Evaluation {i}: reason is required")
        if verdict == CURATOR_VERDICT_PROMOTE:
            fact = ev.get("fact")
            if not fact:
                errors.append(f"Evaluation {i}: promote verdict requires a non-empty fact")
            elif len(fact) < _MIN_PROMOTED_FACT_LEN:
                errors.append(f"Evaluation {i}: promoted fact too short ({len(fact)} chars, min {_MIN_PROMOTED_FACT_LEN})")
        if verdict == CURATOR_VERDICT_PROMOTE and ev.get("category") is not None:
            if ev["category"] not in _VALID_FACT_CATEGORIES:
                errors.append(f"Evaluation {i}: category must be one of {sorted(_VALID_FACT_CATEGORIES)}")
        if verdict == CURATOR_VERDICT_ASK and not ev.get("question"):
            errors.append(f"Evaluation {i}: ask verdict requires a non-empty question")
        # entity required for promote
        if verdict == CURATOR_VERDICT_PROMOTE:
            if not ev.get("entity_name"):
                errors.append(f"Evaluation {i}: promoted fact must have entity_name")
            kind = ev.get("entity_kind")
            if not kind or kind not in _ENTITY_KINDS:
                errors.append(f"Evaluation {i}: promoted fact must have valid entity_kind")
    return errors


def _select_curator_modules() -> list[str]:
    """Select prompt modules for the curator.

    Always includes ``entity_assignment`` (needed for any promote) and
    ``tag_reuse`` (tag formatting/semantic-retrieval guidance).  When no
    existing tags are available the "check existing tags first" instruction
    is a harmless no-op; the formatting rules ("lowercase, hyphenated")
    are always valuable and ensure promoted facts get well-formed tags.
    """
    return ["entity_assignment", "tag_reuse"]


def build_curator_messages(
    learnings: list[dict],
    available_tags: list[str] | None = None,
    available_entities: list[dict] | None = None,
    existing_facts: list[dict] | None = None,
) -> list[dict]:
    """Build the message list for the curator LLM call."""
    modules = _select_curator_modules()
    system_prompt = _load_modular_prompt("curator", modules)
    items = "\n".join(
        f"{i}. [id={l['id']}] {l['content']}"
        for i, l in enumerate(learnings, 1)
    )
    parts = [f"## Learnings\n{items}"]
    _add_section(parts, "Existing Tags", ", ".join(available_tags) if available_tags else "")
    if available_entities:
        entity_lines = "\n".join(f"{e['name']} ({e['kind']})" for e in available_entities)
        parts.append(f"## Existing Entities\n{entity_lines}")
    if existing_facts:
        fact_lines = "\n".join(
            f"[entity: {f.get('entity_name', '?')}] {f['content']}" for f in existing_facts
        )
        parts.append(f"## Existing Facts (already in knowledge base)\n{fact_lines}")
    return _build_messages(system_prompt, "\n\n".join(parts))


async def run_curator(
    config: Config,
    learnings: list[dict],
    session: str = "",
    available_tags: list[str] | None = None,
    available_entities: list[dict] | None = None,
    existing_facts: list[dict] | None = None,
) -> dict:
    """Run the curator on pending learnings.

    Returns dict with key "evaluations".
    Raises CuratorError if all retries exhausted.
    """
    messages = build_curator_messages(
        learnings, available_tags=available_tags,
        available_entities=available_entities,
        existing_facts=existing_facts,
    )
    expected = len(learnings)
    result = await _retry_llm_with_validation(
        config, "curator", messages, CURATOR_SCHEMA,
        lambda r: validate_curator(r, expected_count=expected),
        CuratorError, "Curator",
        session=session,
    )
    log.info("Curator: %d evaluations", len(result["evaluations"]))
    return result


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------

def build_summarizer_messages(
    current_summary: str, messages: list[dict]
) -> list[dict]:
    """Build the message list for the summarizer LLM call."""
    system_prompt = _load_system_prompt("summarizer-session")
    parts: list[str] = []
    _add_section(parts, "Current Summary", current_summary)
    parts.append(f"## Messages\n{build_recent_context(messages, kiso_truncate=0)}")
    return _build_messages(system_prompt, "\n\n".join(parts))


async def _call_role(
    config: Config, role: str, messages: list[dict],
    error_class: type[Exception], session: str = "",
    fallback_model: str | None = None,
) -> str:
    """Call an LLM role and wrap errors in the role-specific exception.

    On ``LLMStallError``, retries once with *fallback_model* if provided.
    """
    try:
        return await call_llm(config, role, messages, session=session)
    except LLMStallError:
        if fallback_model:
            log.warning("SSE stall on %s, trying fallback %s", role, fallback_model)
            try:
                return await call_llm(
                    config, role, messages, session=session,
                    model_override=fallback_model,
                )
            except LLMError as e2:
                raise error_class(f"Fallback LLM call failed: {e2}")
        raise error_class("LLM stall with no fallback model")
    except LLMError as e:
        raise error_class(f"LLM call failed: {e}")


async def run_summarizer(
    config: Config, current_summary: str, messages: list[dict], session: str = "",
) -> str:
    """Run the summarizer. Returns the new summary string."""
    msgs = build_summarizer_messages(current_summary, messages)
    return await _call_role(config, "summarizer", msgs, SummarizerError, session)


# ---------------------------------------------------------------------------
# Paraphraser
# ---------------------------------------------------------------------------


class ParaphraserError(Exception):
    """Paraphraser generation failure."""


def build_paraphraser_messages(messages: list[dict]) -> list[dict]:
    """Build the message list for the paraphraser LLM call."""
    system_prompt = _load_system_prompt("paraphraser")
    lines = []
    for m in messages:
        user = m.get("user") or "unknown"
        content = m.get("content", "")
        lines.append(f"[{user}]: {content}")
    return _build_messages(system_prompt, "\n".join(lines))


async def run_paraphraser(config: Config, messages: list[dict], session: str = "") -> str:
    """Run the paraphraser on untrusted messages. Returns paraphrased text."""
    msgs = build_paraphraser_messages(messages)
    return await _call_role(config, "paraphraser", msgs, ParaphraserError, session)


# ---------------------------------------------------------------------------
# Messenger
# ---------------------------------------------------------------------------


class ExecTranslatorError(Exception):
    """Exec-to-shell translation failure."""


class _ExecTranslatorValidationError(Exception):
    """Internal translator validation error with optional targeted-repair metadata."""

    def __init__(self, message: str, *, repair_kind: str | None = None):
        super().__init__(message)
        self.repair_kind = repair_kind


class MessengerError(Exception):
    """Messenger generation failure."""


def build_messenger_messages(
    config: Config,
    summary: str,
    facts: list[dict],
    detail: str,
    plan_outputs_text: str = "",
    goal: str = "",
    recent_messages: list[dict] | None = None,
    user_message: str = "",
    briefing_context: str | None = None,
    behavior_rules: list[str] | None = None,
) -> list[dict]:
    """Build the message list for the messenger LLM call.

    Args:
        config: Application config (reads bot_name from settings).
        summary: Current session summary.
        facts: Known facts from the knowledge base.
        detail: The msg task detail (what to communicate).
        plan_outputs_text: Pre-formatted preceding task outputs (from worker).
        goal: The plan goal (user's original request for this turn).
        recent_messages: Recent conversation messages (for chat mode context).
        user_message: The original user message (for language/context inference).
        briefing_context: Synthesized context from the briefer (replaces
            raw summary/facts when provided).
    """
    bot_name = config.settings["bot_name"]
    bot_persona = config.settings.get("bot_persona", "a friendly and knowledgeable assistant")
    system_prompt = _load_system_prompt("messenger").replace(
        "{bot_name}", bot_name,
    ).replace(
        "{bot_persona}", bot_persona,
    )

    context_parts: list[str] = []
    # extract language from "Answer in {lang}." prefix and inject as
    # a dedicated top-level section so the LLM cannot miss it.
    _lang_m = _ANSWER_IN_LANG_RE.match(detail)
    if _lang_m:
        context_parts.append(
            f"## Language Directive\nRespond entirely in **{_lang_m.group(1)}**."
        )
    if user_message:
        context_parts.append(
            f"## Original User Message\n{fence_content(user_message, 'USER_MSG')}"
        )
    _add_section(context_parts, "Current User Request", goal)
    if briefing_context:
        # Briefer path: synthesized context replaces raw summary/facts.
        # Fence LLM-generated briefer output to prevent cross-LLM injection.
        context_parts.append(f"## Context\n{fence_content(briefing_context, 'BRIEFER_CONTEXT')}")
    else:
        # Fallback: full raw context
        _add_section(context_parts, "Session Summary (background only)", summary)
        _add_section(context_parts, "Known Facts",
                     _join_or_empty(facts, lambda f: f"- {f['content']}"))
    if recent_messages:
        context_parts.append(
            f"## Recent Conversation\n{fence_content(build_recent_context(recent_messages, kiso_truncate=0), 'MESSAGES')}"
        )
    # inject behavioral guidelines
    if behavior_rules:
        _add_section(context_parts, "Behavior Guidelines (follow these preferences)",
                     "\n".join(f"- {r}" for r in behavior_rules))
    _add_section(context_parts, "Preceding Task Outputs", plan_outputs_text)
    context_parts.append(f"## Task\n{detail}")
    return _build_messages(system_prompt, "\n\n".join(context_parts))


async def run_messenger(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    detail: str,
    plan_outputs_text: str = "",
    goal: str = "",
    include_recent: bool = False,
    user_message: str = "",
    briefing_context: str | None = None,
) -> str:
    """Run the messenger: generate a user-facing response.

    Loads session summary and facts, builds context, and calls the
    messenger LLM to produce text for the user.

    When *briefing_context* is provided (from the briefer), it replaces
    the raw summary and facts in the messenger prompt.

    Returns the generated text.
    Raises MessengerError on failure.
    """
    summary = ""
    facts: list[dict] = []
    if not briefing_context:
        # Only fetch summary/facts when briefer hasn't already filtered them
        sess = await get_session(db, session)
        summary = sess["summary"] if sess else ""
        facts = await get_facts(db, session=session, limit=_MAX_MESSENGER_FACTS)
    recent = None
    if include_recent:
        context_limit = int(config.settings["context_messages"])
        recent = await get_recent_messages(db, session, limit=context_limit)
    # fetch behavior guidelines for messenger
    behavior_facts = await get_behavior_facts(db)
    behavior_rules = [f["content"] for f in behavior_facts] if behavior_facts else None
    messages = build_messenger_messages(
        config, summary, facts, detail, plan_outputs_text, goal=goal,
        recent_messages=recent or None, user_message=user_message,
        briefing_context=briefing_context, behavior_rules=behavior_rules,
    )
    # retry messenger LLM call up to 2 times on transient errors.
    # on SSE stall, switch to fallback model immediately (don't waste retries).
    _fallback = config.settings.get("planner_fallback_model", "minimax/minimax-m2.7")
    _using_fallback = False
    _last_err: LLMError | None = None
    for _attempt in range(_MAX_MESSENGER_RETRIES + 1):
        try:
            model_override = _fallback if _using_fallback else None
            text = await call_llm(
                config, "messenger", messages, session=session,
                model_override=model_override,
            )
            return _sanitize_messenger_output(text)
        except LLMBudgetExceeded:
            raise  # non-retryable — budget is exhausted
        except LLMStallError as e:
            if not _using_fallback and _fallback:
                log.warning("Messenger SSE stall, switching to fallback %s", _fallback)
                _using_fallback = True
                continue  # retry immediately with fallback
            _last_err = e
            break  # already on fallback or no fallback — give up
        except LLMError as e:
            _last_err = e
            if _attempt < _MAX_MESSENGER_RETRIES:
                log.warning("Messenger retry %d/%d: %s", _attempt + 1, _MAX_MESSENGER_RETRIES, e)
                if _MESSENGER_RETRY_BACKOFF > 0:
                    await asyncio.sleep(_MESSENGER_RETRY_BACKOFF)
                continue
    raise MessengerError(f"LLM call failed after {_MAX_MESSENGER_RETRIES + 1} attempts: {_last_err}")


# strip hallucinated XML/tool markup from messenger output
_TOOL_CALL_BLOCK_RE = re.compile(
    r"<(tool_call|function_call)[^>]*>.*?</\1>", re.DOTALL,
)
_TOOL_CALL_TAG_RE = re.compile(r"</?(tool_call|function_call)[^>]*>")


def _sanitize_messenger_output(text: str) -> str:
    """Strip hallucinated tool_call/function_call XML from messenger output."""
    cleaned = _TOOL_CALL_BLOCK_RE.sub("", text)
    cleaned = _TOOL_CALL_TAG_RE.sub("", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


class SearcherError(Exception):
    """Searcher generation failure."""


def build_searcher_messages(
    query: str,
    context: str = "",
    max_results: int | None = None,
    lang: str | None = None,
    country: str | None = None,
) -> list[dict]:
    """Build the message list for the searcher LLM call."""
    system_prompt = _load_system_prompt("searcher")
    parts = [f"## Search Query\n{query}"]
    params: list[str] = []
    if max_results is not None:
        params.append(f"max_results: {max_results}")
    if lang:
        params.append(f"lang: {lang}")
    if country:
        params.append(f"country: {country}")
    if params:
        parts.append("## Search Parameters\n" + "\n".join(params))
    _add_section(parts, "Context", context)
    return _build_messages(system_prompt, "\n\n".join(parts))


async def run_searcher(
    config: Config,
    query: str,
    context: str = "",
    max_results: int | None = None,
    lang: str | None = None,
    country: str | None = None,
    session: str = "",
) -> str:
    """Run the searcher: web search via an online-capable model.

    Returns the raw search results text (not parsed).
    Raises SearcherError on failure.
    """
    messages = build_searcher_messages(
        query, context, max_results=max_results, lang=lang, country=country,
    )
    return await _call_role(config, "searcher", messages, SearcherError, session)


# ---------------------------------------------------------------------------
# Exec translator  (planner = architect, worker/translator = editor)
# ---------------------------------------------------------------------------

def build_exec_translator_messages(
    config: Config,
    detail: str,
    sys_env_text: str,
    plan_outputs_text: str = "",
    retry_context: str = "",
    workspace_files: str = "",
) -> list[dict]:
    """Build the message list for the exec translator LLM call."""
    system_prompt = _load_system_prompt("worker")
    context_parts: list[str] = [f"## System Environment\n{sys_env_text}"]
    _add_section(context_parts, "Workspace Files", workspace_files)
    _add_section(context_parts, "Preceding Task Outputs", plan_outputs_text)
    _add_section(context_parts, "Retry Context", retry_context)
    context_parts.append(f"## Task\n{detail}")
    return _build_messages(system_prompt, "\n\n".join(context_parts))


def _is_simple_shell_intent(detail: str) -> bool:
    """True for obvious one-step shell intents where scripts are overkill."""
    detail_l = detail.lower().strip()
    simple_markers = (
        "current working directory", "working directory",
        "list all files", "list files", "list directories",
        "print the text", "print text", "echo ",
        "show the contents", "show contents", "cat ",
        "check if", "is installed", "command -v",
    )
    return any(marker in detail_l for marker in simple_markers)


def _build_exec_translator_repair_context(
    detail: str,
    *,
    error_text: str,
    repair_kind: str,
    previous_command: str,
    retry_context: str = "",
) -> str:
    """Build a bounded targeted retry hint for structural translator failures."""
    parts: list[str] = []
    if retry_context.strip():
        parts.append(retry_context.strip())
    parts.append(
        "Targeted repair: the previous translator output was structurally invalid. "
        "Return ONLY the corrected shell command."
    )
    parts.append(f"Previous invalid output:\n{previous_command}")
    parts.append(f"Validation error: {error_text}")
    if repair_kind == "syntax":
        parts.append("Fix the bash syntax error and return the shortest equivalent valid shell command.")
    elif repair_kind == "fences":
        parts.append("Remove markdown fences/comments and return raw shell commands only.")
    elif repair_kind == "natural_language":
        parts.append("Replace the natural-language explanation with the actual shell command only.")
    if _is_simple_shell_intent(detail):
        parts.append("This is a simple one-step task. Prefer a single direct command, not a script or heredoc.")
    parts.append("Never repeat the invalid format.")
    return "\n\n".join(parts)


def _validate_exec_translator_command(command: str) -> None:
    """Validate translator output and raise targeted repair errors when possible."""
    if not command or command == "CANNOT_TRANSLATE":
        raise ExecTranslatorError("Cannot translate task to shell command")

    if "```" in command:
        raise _ExecTranslatorValidationError(
            "Markdown fences in command output",
            repair_kind="fences",
        )

    _ECHO_MARKERS = (
        "Public files:", "Blocked commands:", "Plan limits:",
        "Exec CWD:", "System Environment", "Preceding Task Outputs",
        "## Task", "Available binaries:",
    )
    for marker in _ECHO_MARKERS:
        if marker in command:
            raise ExecTranslatorError(
                f"Prompt echo-back detected ('{marker}' in output)"
            )

    _NL_PREFIXES = (
        "I ", "The ", "Here ", "To ", "Let me", "This ", "Sure",
        "Based on", "First,", "Note:", "Unfortunately",
    )
    first_line = command.split("\n", 1)[0]
    if any(first_line.startswith(p) for p in _NL_PREFIXES):
        raise _ExecTranslatorValidationError(
            f"Natural language in command output: {first_line[:80]}",
            repair_kind="natural_language",
        )


async def run_exec_translator(
    config: Config,
    detail: str,
    sys_env_text: str,
    plan_outputs_text: str = "",
    session: str = "",
    retry_context: str = "",
    workspace_files: str = "",
) -> str:
    """Translate a natural-language exec task detail into a shell command.

    Returns the shell command string.
    Raises ExecTranslatorError on failure.
    """
    _fallback = config.settings.get("planner_fallback_model", "minimax/minimax-m2.7")
    current_retry_context = retry_context
    for attempt in range(2):
        messages = build_exec_translator_messages(
            config, detail, sys_env_text, plan_outputs_text,
            retry_context=current_retry_context,
            workspace_files=workspace_files,
        )
        raw = await _call_role(
            config, "worker", messages, ExecTranslatorError, session,
            fallback_model=_fallback,
        )
        command = raw.strip()
        try:
            _validate_exec_translator_command(command)

            # M1058: always run bash -n syntax check (was >120 chars only)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-n",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate(input=command.encode())
                if proc.returncode != 0:
                    hint = stderr.decode(errors="replace").strip()
                    raise _ExecTranslatorValidationError(
                        f"Bash syntax error in generated command: {hint}",
                        repair_kind="syntax",
                    )
            except FileNotFoundError:
                pass  # bash not available — skip check
            return command
        except _ExecTranslatorValidationError as e:
            if attempt == 0 and e.repair_kind in {"syntax", "fences", "natural_language"}:
                current_retry_context = _build_exec_translator_repair_context(
                    detail,
                    error_text=str(e),
                    repair_kind=e.repair_kind,
                    previous_command=command,
                    retry_context=retry_context,
                )
                continue
            raise ExecTranslatorError(str(e)) from e
        except ExecTranslatorError as e:
            if "Cannot translate task to shell command" in str(e):
                raise ExecTranslatorError(
                    f"Cannot translate task to shell command: {detail}"
                ) from e
            raise

    raise ExecTranslatorError(f"Cannot translate task to shell command: {detail}")


# ---------------------------------------------------------------------------
# Dreamer (periodic knowledge consolidation)
# ---------------------------------------------------------------------------


class ConsolidatorError(Exception):
    """Consolidator validation or generation failure."""


CONSOLIDATOR_SCHEMA: dict = _build_strict_schema("consolidator", {
    "delete": {"type": "array", "items": {"type": "integer"}},
    "update": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "content": {"type": "string"},
        },
        "required": ["id", "content"],
        "additionalProperties": False,
    }},
    "keep": {"type": "array", "items": {"type": "integer"}},
}, ["delete", "update", "keep"])


def build_consolidator_messages(facts_by_entity: dict[str, list[dict]]) -> list[dict]:
    """Build the message list for the consolidator LLM call.

    *facts_by_entity* maps entity name (or "(no entity)") to a list of
    fact dicts, each with at least ``id`` and ``content``.
    """
    system_prompt = _load_system_prompt("consolidator")
    parts: list[str] = []
    for entity_name, facts in sorted(facts_by_entity.items()):
        lines = "\n".join(f"  [{f['id']}] {f['content']}" for f in facts)
        parts.append(f"### {entity_name}\n{lines}")
    user_content = "## Stored Facts\n\n" + "\n\n".join(parts)
    return _build_messages(system_prompt, user_content)


def validate_consolidator(result: dict, expected_ids: set[int]) -> list[str]:
    """Validate consolidator result. Returns list of error strings (empty = valid)."""
    errors: list[str] = []
    delete_ids = set(result.get("delete", []))
    update_ids = {item["id"] for item in result.get("update", [])}
    keep_ids = set(result.get("keep", []))

    all_mentioned = delete_ids | update_ids | keep_ids
    # Check for overlap between categories
    if delete_ids & update_ids:
        errors.append(f"IDs in both delete and update: {sorted(delete_ids & update_ids)}")
    if delete_ids & keep_ids:
        errors.append(f"IDs in both delete and keep: {sorted(delete_ids & keep_ids)}")
    if update_ids & keep_ids:
        errors.append(f"IDs in both update and keep: {sorted(update_ids & keep_ids)}")

    missing = expected_ids - all_mentioned
    if missing:
        errors.append(f"Missing fact IDs: {sorted(missing)}")
    extra = all_mentioned - expected_ids
    if extra:
        errors.append(f"Unknown fact IDs: {sorted(extra)}")

    # Check update items have non-empty content
    for item in result.get("update", []):
        if not item.get("content", "").strip():
            errors.append(f"Update for fact {item['id']} has empty content")

    return errors


def _group_facts_by_entity(
    facts: list[dict], entities: list[dict],
) -> dict[str, list[dict]]:
    """Group facts by entity name. Facts without entity go under '(no entity)'."""
    entity_map = {e["id"]: e["name"] for e in entities}
    grouped: dict[str, list[dict]] = {}
    for f in facts:
        name = entity_map.get(f.get("entity_id")) or "(no entity)"
        grouped.setdefault(name, []).append(f)
    return grouped


async def run_consolidator(
    config: Config, db: aiosqlite.Connection, session: str = "",
) -> dict:
    """Run the consolidator on all stored facts.

    Returns dict with keys: delete, update, keep.
    Raises ConsolidatorError if all retries exhausted.
    """
    all_facts = await get_facts(db, is_admin=True)
    if not all_facts:
        return {"delete": [], "update": [], "keep": []}

    entities = await get_all_entities(db)
    facts_by_entity = _group_facts_by_entity(all_facts, entities)
    messages = build_consolidator_messages(facts_by_entity)
    expected_ids = {f["id"] for f in all_facts}

    result = await _retry_llm_with_validation(
        config, "consolidator", messages, CONSOLIDATOR_SCHEMA,
        lambda r: validate_consolidator(r, expected_ids),
        ConsolidatorError, "Consolidator",
        session=session,
    )
    log.info(
        "Consolidator: delete=%d update=%d keep=%d",
        len(result["delete"]), len(result["update"]), len(result["keep"]),
    )
    return result


async def apply_consolidation_result(db: aiosqlite.Connection, result: dict) -> None:
    """Apply consolidator result: delete and update facts."""
    # Deletions
    to_delete = result.get("delete", [])
    if to_delete:
        await delete_facts(db, to_delete)

    # Updates
    for item in result.get("update", []):
        content = item.get("content", "").strip()
        if content:
            await update_fact_content(db, item["id"], content)
