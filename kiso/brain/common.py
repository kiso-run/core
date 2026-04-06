"""Shared helpers and non-role-specific logic for `kiso.brain`."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiosqlite

from . import prompts as _prompting
from .prompts import (
    _ANSWER_IN_LANG_RE,
    _add_context_section,
    _add_section,
    _build_messages,
    _build_messages_from_sections,
)
from kiso.config import Config, KISO_DIR, setting_bool, setting_int
from kiso.llm import LLMBudgetExceeded, LLMError, LLMStallError, call_llm
from kiso.registry import get_registry_tools
from kiso.security import fence_content
from kiso.connectors import discover_connectors
from kiso.recipe_loader import (
    build_recipe_runtime_contracts_text,
    discover_recipes,
    build_planner_recipe_list,
    filter_recipes_for_message,
)
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

if TYPE_CHECKING:
    from kiso.worker.utils import ExecutionState

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
_INSTALL_MODE_UNKNOWN_KISO_TOOL = "unknown_kiso_tool"
_INSTALL_MODE_PYTHON_LIB = "python_lib"
_INSTALL_MODE_SYSTEM_PKG = "system_pkg"
_INSTALL_TARGET_RE = re.compile(
    r"\b(?:install|installa|installare|installer)\b"
    r"(?:\s+(?:the|a|an|il|lo|la|i|gli|le|un|una))?"
    r"(?:\s+(?:kiso\s+tool|tool|plugin|package|pkg|pacchetto|libreria|library|module|modulo|python\s+package|python\s+library|python\s+module|system\s+package|pacchetto\s+di\s+sistema))?"
    r"\s+['\"`]?"
    r"([a-z0-9][a-z0-9._+-]{0,63})"
    r"['\"`]?\b",
    re.IGNORECASE,
)
_NAMED_TOOL_TARGET_RE = re.compile(
    r"\b(?:kiso\s+tool|tool|plugin|connector|skill)\b\s+['\"`]([a-z0-9][a-z0-9._+-]{0,63})['\"`]",
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
_KISO_TOOL_SIGNAL_RE = re.compile(
    r"\b(?:kiso\s+tool|kiso\s+plugin|plugin|connector|skill|registry)\b",
    re.IGNORECASE,
)
_COMMON_PYTHON_PACKAGES = frozenset({
    "aiohttp", "aiosqlite", "anthropic", "beautifulsoup4", "black", "bs4",
    "celery", "click", "django", "fastapi", "flask", "httpx", "jinja2",
    "langchain", "lxml", "matplotlib", "numpy", "openai", "pandas",
    "playwright", "pydantic", "pytest", "requests", "rich", "scipy",
    "seaborn", "sqlalchemy", "streamlit", "tenacity", "tomli", "uvicorn",
})
_GENERIC_INSTALL_TARGETS = frozenset({
    "one", "it", "this", "that", "them", "something", "anything",
})


def _normalize_install_target_token(token: str | None) -> str | None:
    """Normalize install target tokens extracted from free-form text."""
    if not token:
        return None
    cleaned = token.strip().strip("`'\".,;:!?)]}")
    return cleaned.lower() or None


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
    named_match = _NAMED_TOOL_TARGET_RE.search(message)
    if named_match and any(kw in message.lower() for kw in _INSTALL_KEYWORDS):
        target = _normalize_install_target_token(named_match.group(1))
        if target in _GENERIC_INSTALL_TARGETS:
            return None
        return target
    match = _INSTALL_TARGET_RE.search(message)
    if not match:
        return None
    target = _normalize_install_target_token(match.group(1))
    if target in _GENERIC_INSTALL_TARGETS:
        return None
    return target


def _is_explicit_named_tool_request(message: str, target: str) -> bool:
    """Return True when the user explicitly frames *target* as a named tool/plugin."""
    if not target:
        return False
    if _KISO_TOOL_SIGNAL_RE.search(message):
        return True
    escaped = re.escape(target)
    return bool(re.search(rf"\btool\s+['\"`]?{escaped}['\"`]?\b", message, re.IGNORECASE))


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
            "target_installed": target in installed_names,
            "reason": "target matches kiso tool context",
        }

    if _is_explicit_named_tool_request(message, target):
        return {
            "mode": _INSTALL_MODE_UNKNOWN_KISO_TOOL,
            "target": target,
            "reason": "user explicitly requested a named tool/plugin not present in current kiso tool context",
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
    elif mode == _INSTALL_MODE_UNKNOWN_KISO_TOOL:
        lines.append("Route: unknown named tool/plugin request — msg only.")
        lines.append("Do not set needs_install and do not use apt-get, uv pip install, or kiso tool install.")
        lines.append("Explain that the named tool is not available in the current registry/tool context and ask for a git URL or installation instructions if it is private.")
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

VALIDATION_RETRY_TASK_REPAIR = "task_repair"
VALIDATION_RETRY_PLAN_REWRITE = "plan_rewrite"
VALIDATION_RETRY_APPROACH_RESET = "approach_reset"
VALIDATION_RETRY_CLASSES: frozenset[str] = frozenset({
    VALIDATION_RETRY_TASK_REPAIR,
    VALIDATION_RETRY_PLAN_REWRITE,
    VALIDATION_RETRY_APPROACH_RESET,
})

_PLAN_REWRITE_ERROR_PATTERNS = (
    "plan has only msg tasks",
    "last task must be type",
    "goal mentions creating a file/document but plan has no exec or tool task",
    "needs_install is set",
    "a plan can have at most one replan task",
    "group ",
    "installs a tool/connector in the first plan",
    "plan installs a tool after user approval but ends with msg",
)
_APPROACH_RESET_ERROR_PATTERNS = (
    "the requested tool does not exist in any registry",
    "plan only msg tasks explaining the situation to the user",
    "cannot be found in the public registry",
)
_TASK_REPAIR_ERROR_PATTERNS = (
    "tool args invalid",
    "tool args is not valid json",
    "tool args must be a json object",
    "missing required arg:",
    "must have expect = null",
    "must have tool = null",
    "must have args = null",
)

_ACTION_TO_USER_RE = re.compile(
    r"\b("
    r"tell me|send me|show me|report back|reply with|let me know|"
    r"tell the user|send the user|show the user|report to the user|"
    r"dimmi|mandami|mostrami|fammi sapere|inviami|dillo all'utente|"
    r"manda all'utente|mostra all'utente|riporta all'utente"
    r")\b",
    re.IGNORECASE,
)

FAILURE_CLASS_TASK_SHAPE = "task_shape_validation"
FAILURE_CLASS_SEMANTIC_TOOL = "semantic_tool_validation"
FAILURE_CLASS_WORKSPACE_ROUTING = "workspace_file_routing"
FAILURE_CLASS_BLOCKED_POLICY = "blocked_command_policy"
FAILURE_CLASS_EXTERNAL_DEP = "external_dependency"
FAILURE_CLASS_PLAN_SHAPE = "plan_shape_error"
FAILURE_CLASS_DELIVERY_SPLIT = "final_delivery_split"
FAILURE_CLASSES: frozenset[str] = frozenset({
    FAILURE_CLASS_TASK_SHAPE,
    FAILURE_CLASS_SEMANTIC_TOOL,
    FAILURE_CLASS_WORKSPACE_ROUTING,
    FAILURE_CLASS_BLOCKED_POLICY,
    FAILURE_CLASS_EXTERNAL_DEP,
    FAILURE_CLASS_PLAN_SHAPE,
    FAILURE_CLASS_DELIVERY_SPLIT,
})


def _is_plugin_discovery_search(detail: str) -> bool:
    """Return True if detail looks like a plugin discovery search query."""
    return bool(_PLUGIN_DISCOVERY_RE.search(detail))


def _mentions_user_delivery(detail: str) -> bool:
    """Return True when text includes user-delivery wording."""
    return bool(_ACTION_TO_USER_RE.search(detail or ""))


def classify_failure_class(errors_or_reason: list[str] | str | None) -> str:
    """Map validation/review/runtime failures onto a shared internal class."""
    if isinstance(errors_or_reason, list):
        text = " ".join(errors_or_reason).lower()
    else:
        text = str(errors_or_reason or "").lower()

    if (
        "tool args validation failed" in text
        or "tool args invalid" in text
        or "tool args is not valid json" in text
        or "tool args must be a json object" in text
        or "missing required arg:" in text
        or "files must contain file paths only" in text
    ):
        return FAILURE_CLASS_SEMANTIC_TOOL
    if (
        "workspace file" in text
        or "module not found" in text
        or "no module named" in text
        or "no such file" in text
        or "cannot find" in text and "file" in text
    ):
        return FAILURE_CLASS_WORKSPACE_ROUTING
    if (
        "blocked by safety rule" in text
        or "tool installation blocked" in text
        or "blocked by pre-exec hook" in text
        or "command blocked" in text
    ):
        return FAILURE_CLASS_BLOCKED_POLICY
    if (
        "timed out" in text
        or "rate limit" in text
        or "api down" in text
        or "executable not found" in text
        or "cannot translate task to shell command" in text
        or "search failed:" in text
        or "review failed" in text
    ):
        return FAILURE_CLASS_EXTERNAL_DEP
    if _mentions_user_delivery(text):
        return FAILURE_CLASS_DELIVERY_SPLIT
    if any(pattern in text for pattern in _PLAN_REWRITE_ERROR_PATTERNS):
        return FAILURE_CLASS_PLAN_SHAPE
    if any(pattern in text for pattern in _TASK_REPAIR_ERROR_PATTERNS):
        return FAILURE_CLASS_TASK_SHAPE
    if "task " in text and ("must have" in text or "unknown type" in text):
        return FAILURE_CLASS_TASK_SHAPE
    return FAILURE_CLASS_PLAN_SHAPE


def _classify_validation_errors(errors: list[str]) -> str:
    """Classify planner validation failures by recovery scope."""
    joined = " ".join(errors).lower()
    if any(pattern in joined for pattern in _APPROACH_RESET_ERROR_PATTERNS):
        return VALIDATION_RETRY_APPROACH_RESET
    failure_class = classify_failure_class(errors)
    if failure_class in {FAILURE_CLASS_TASK_SHAPE, FAILURE_CLASS_SEMANTIC_TOOL}:
        return VALIDATION_RETRY_TASK_REPAIR
    if failure_class in {FAILURE_CLASS_PLAN_SHAPE, FAILURE_CLASS_DELIVERY_SPLIT}:
        return VALIDATION_RETRY_PLAN_REWRITE

    task_nums = {
        match.group(1)
        for error in errors
        for match in re.finditer(r"task\s+(\d+):", error, re.IGNORECASE)
    }
    if len(task_nums) == 1:
        return VALIDATION_RETRY_TASK_REPAIR
    if len(task_nums) > 1:
        return VALIDATION_RETRY_PLAN_REWRITE
    return VALIDATION_RETRY_PLAN_REWRITE


def _build_validation_feedback(
    error_noun: str,
    errors: list[str],
    repeat_count: int,
) -> str:
    """Build class-specific feedback for validation retries."""
    classification = _classify_validation_errors(errors)
    error_lines = [f"- {e}" for e in errors]

    if classification == VALIDATION_RETRY_TASK_REPAIR:
        guidance = (
            f"Keep the same goal and overall {error_noun.lower()}. "
            "Fix only the specific task-level issues below."
        )
    elif classification == VALIDATION_RETRY_APPROACH_RESET:
        guidance = (
            "The previous approach is invalid for this user goal. "
            f"Discard it and regenerate the {error_noun.lower()} from the original user request."
        )
    else:
        guidance = (
            f"Keep the same goal, but rewrite the {error_noun.lower()} structure so it is valid. "
            "Do not patch only one field if the task sequence itself is wrong. "
            "For normal action requests, keep at least one exec/tool/search task and end with "
            "a final msg or replan. Do not collapse to msg-only unless the validation errors "
            "explicitly require a msg-only fallback."
        )

    if repeat_count >= 2:
        if classification == VALIDATION_RETRY_TASK_REPAIR:
            escalation = (
                f"IMPORTANT: You have repeated this same error {repeat_count} times. "
                "Apply the exact field/task fix described above."
            )
        elif classification == VALIDATION_RETRY_APPROACH_RESET:
            escalation = (
                f"IMPORTANT: You have repeated this same error {repeat_count} times. "
                "This indicates the same wrong approach. Discard the prior approach "
                "and start again from the original user goal."
            )
        else:
            escalation = (
                f"IMPORTANT: You have repeated this same error {repeat_count} times. "
                "Regenerate the task sequence instead of making a tiny patch."
            )
        error_lines.append("")
        error_lines.append(escalation)

    return (
        f"Your {error_noun.lower()} has errors:\n"
        + "\n".join(error_lines)
        + f"\n{guidance}\nReturn only the corrected {error_noun.lower()}."
    )

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
            error_feedback = _build_validation_feedback(
                error_noun, last_errors, repeat_count
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
            "args": {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
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

_MIN_PROMOTED_FACT_LEN = 10

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


@dataclass(slots=True)
class MemoryPack:
    """Role-specific memory payload assembled before prompt rendering."""

    role: str
    context_sections: dict[str, str] = field(default_factory=dict)
    operational_sections: dict[str, str] = field(default_factory=dict)
    semantic_sections: dict[str, str] = field(default_factory=dict)
    facts: list[dict] = field(default_factory=list)
    recent_messages: list[dict] = field(default_factory=list)
    behavior_rules: list[str] = field(default_factory=list)
    available_tags: list[str] = field(default_factory=list)
    available_entities: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.context_sections:
            self.context_sections = _merge_context_sections(
                self.semantic_sections,
                self.operational_sections,
                owner=f"{self.role}-memory",
            )


@dataclass(slots=True)
class PlannerPromptState:
    """Canonical planner prompt input built from memory + execution state."""

    summary: str
    facts: list[dict]
    pending: list[dict]
    recent: list[dict]
    memory_pack: MemoryPack
    execution_state: "ExecutionState"
    context_sections: dict[str, str] = field(default_factory=dict)
    sys_env_essential: str = ""
    sys_env_full: str = ""
    install_context: str = ""


def _merge_context_sections(*section_maps: dict[str, str], owner: str) -> dict[str, str]:
    """Merge prompt sections while rejecting conflicting duplicate values."""
    merged: dict[str, str] = {}
    for section_map in section_maps:
        for key, value in section_map.items():
            if not value:
                continue
            existing = merged.get(key)
            if existing is not None and existing != value:
                raise ValueError(
                    f"{owner} prompt section '{key}' diverged between structured sources",
                )
            merged[key] = value
    return merged


def _require_memory_pack_role(memory_pack: MemoryPack, role: str) -> None:
    """Guard against wiring the wrong structured memory pack into a role."""
    if memory_pack.role != role:
        raise ValueError(
            f"Expected memory pack role '{role}', got '{memory_pack.role}'",
        )


def _build_planner_memory_pack(
    *,
    summary: str,
    facts_text: str,
    pending_text: str,
    recent_text: str,
    paraphrased_context: str | None = None,
) -> MemoryPack:
    """Assemble planner-specific memory from the current store fetches."""
    operational_sections: dict[str, str] = {}
    semantic_sections: dict[str, str] = {}
    if summary:
        operational_sections["summary"] = summary
    if facts_text:
        semantic_sections["facts"] = facts_text
    if pending_text:
        operational_sections["pending"] = pending_text
    if recent_text:
        operational_sections["recent_messages"] = recent_text
    if paraphrased_context:
        operational_sections["paraphrased"] = paraphrased_context
    return MemoryPack(
        role="planner",
        operational_sections=operational_sections,
        semantic_sections=semantic_sections,
    )


def _build_messenger_memory_pack(
    *,
    summary: str,
    facts: list[dict],
    recent_messages: list[dict] | None,
    behavior_rules: list[str] | None,
) -> MemoryPack:
    """Assemble messenger-specific memory."""
    operational_sections: dict[str, str] = {}
    if summary:
        operational_sections["summary"] = summary
    return MemoryPack(
        role="messenger",
        operational_sections=operational_sections,
        facts=list(facts),
        recent_messages=list(recent_messages or []),
        behavior_rules=list(behavior_rules or []),
    )


def _build_curator_memory_pack(
    *,
    available_tags: list[str] | None,
    available_entities: list[dict] | None,
) -> MemoryPack:
    """Assemble curator-specific memory."""
    return MemoryPack(
        role="curator",
        available_tags=list(available_tags or []),
        available_entities=list(available_entities or []),
    )


def _build_worker_memory_pack(
    *,
    summary: str = "",
    facts: list[dict] | None = None,
    recent_message: str = "",
    plan_outputs_text: str = "",
    goal: str = "",
    available_tags: list[str] | None = None,
    available_entities: list[dict] | None = None,
) -> MemoryPack:
    """Assemble worker-side memory used by messenger briefing."""
    operational_sections: dict[str, str] = {}
    semantic_sections: dict[str, str] = {}
    if plan_outputs_text:
        operational_sections["plan_outputs"] = plan_outputs_text
    if goal:
        operational_sections["goal"] = goal
    if recent_message:
        operational_sections["recent_messages"] = recent_message
    if summary:
        operational_sections["summary"] = summary
    if facts:
        semantic_sections["facts"] = "\n".join(f"- {f['content']}" for f in facts)
    if available_tags:
        semantic_sections["available_tags"] = ", ".join(available_tags)
    if available_entities:
        semantic_sections["available_entities"] = "\n".join(
            f"{e['name']} ({e['kind']})" for e in available_entities
        )
    return MemoryPack(
        role="worker",
        operational_sections=operational_sections,
        semantic_sections=semantic_sections,
        facts=list(facts or []),
        available_tags=list(available_tags or []),
        available_entities=list(available_entities or []),
    )


class BrieferError(Exception):
    """Briefer generation failure."""


class ReviewError(Exception):
    """Review validation or generation failure."""


class PlanError(Exception):
    """Plan validation or generation failure."""


def _load_system_prompt(role: str) -> str:
    """Compatibility wrapper over the extracted prompt loader."""
    _prompting.KISO_DIR = KISO_DIR
    return _prompting._load_system_prompt(role)


def invalidate_prompt_cache() -> None:
    """Compatibility wrapper over the extracted prompt cache reset."""
    _prompting.invalidate_prompt_cache()


def _load_modular_prompt(role: str, modules: list[str]) -> str:
    """Compatibility wrapper preserving brain-level patch points in tests."""
    return _prompting._render_modular_prompt_text(_load_system_prompt(role), modules)


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

    return _build_messages_from_sections(system_prompt, parts)


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


__brain_exports__ = [
    "BRIEFER_MODULES",
    "BRIEFER_SCHEMA",
    "BrieferError",
    "CLASSIFIER_CATEGORIES",
    "CURATOR_VERDICTS",
    "CURATOR_VERDICT_ASK",
    "CURATOR_VERDICT_DISCARD",
    "CURATOR_VERDICT_PROMOTE",
    "CURATOR_MODULES",
    "ClassifierError",
    "INFLIGHT_CATEGORIES",
    "INFLIGHT_SCHEMA",
    "MemoryPack",
    "PlanError",
    "PLAN_SCHEMA",
    "PlannerPromptState",
    "REVIEW_STATUSES",
    "REVIEW_STATUS_OK",
    "REVIEW_STATUS_REPLAN",
    "REVIEW_STATUS_STUCK",
    "REVIEW_SCHEMA",
    "ReviewError",
    "TASK_TYPE_EXEC",
    "TASK_TYPE_MSG",
    "TASK_TYPE_REPLAN",
    "TASK_TYPE_SEARCH",
    "TASK_TYPE_TOOL",
    "TASK_TYPES",
    "WORKER_PHASE_CLASSIFYING",
    "WORKER_PHASE_EXECUTING",
    "WORKER_PHASE_IDLE",
    "WORKER_PHASE_PLANNING",
    "WORKER_PHASES",
    "_ANSWER_IN_LANG_RE",
    "_BRIEFER_MODULE_DESCRIPTIONS",
    "_INSTALL_CMD_RE",
    "_MAX_MESSENGER_FACTS",
    "_MESSENGER_RETRY_BACKOFF",
    "_MIN_PROMOTED_FACT_LEN",
    "_TOOL_UNAVAILABLE_MARKER",
    "_TOOL_NOT_INSTALLED_MARKER",
    "_VALID_FACT_CATEGORIES",
    "_build_strict_schema",
    "_build_validation_feedback",
    "_build_curator_memory_pack",
    "_build_install_mode_context",
    "_build_messenger_memory_pack",
    "_build_planner_memory_pack",
    "_build_worker_memory_pack",
    "_classify_install_mode",
    "_classify_validation_errors",
    "_compress_install_turns",
    "_CONTEXT_POOL_SECTIONS",
    "_extract_install_target",
    "_filter_briefer_names",
    "_format_message_history",
    "_format_pending_items",
    "_is_plugin_discovery_search",
    "_is_explicit_named_tool_request",
    "_join_or_empty",
    "_load_modular_prompt",
    "_load_system_prompt",
    "_merge_context_sections",
    "_parse_registry_hint_names",
    "_prefilter_context_pool",
    "_repair_json",
    "_require_memory_pack_role",
    "_retry_llm_with_validation",
    "_strip_fences",
    "build_briefer_messages",
    "build_classifier_messages",
    "build_inflight_classifier_messages",
    "build_recent_context",
    "check_safety_rules",
    "classify_failure_class",
    "classify_inflight",
    "classify_message",
    "FAILURE_CLASS_BLOCKED_POLICY",
    "FAILURE_CLASS_DELIVERY_SPLIT",
    "FAILURE_CLASS_PLAN_SHAPE",
    "FAILURE_CLASS_SEMANTIC_TOOL",
    "FAILURE_CLASS_TASK_SHAPE",
    "FAILURE_CLASS_WORKSPACE_ROUTING",
    "invalidate_prompt_cache",
    "is_stop_message",
    "run_briefer",
    "VALIDATION_RETRY_APPROACH_RESET",
    "VALIDATION_RETRY_PLAN_REWRITE",
    "VALIDATION_RETRY_TASK_REPAIR",
    "validate_briefing",
]
