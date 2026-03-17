"""Planner brain — builds context, calls LLM, validates plan."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

import aiosqlite

from kiso.config import Config, KISO_DIR, setting_bool
from kiso.llm import LLMBudgetExceeded, LLMError, LLMStallError, call_llm
from kiso.registry import get_registry_tools
from kiso.security import fence_content
from kiso.skill_loader import discover_md_skills, build_planner_skill_list
from kiso.tools import discover_tools, build_planner_tool_list, validate_tool_args
from kiso.store import (
    _normalize_entity_name,
    get_all_entities, get_all_tags, get_facts, get_pending_items,
    get_behavior_facts, get_recent_messages, get_safety_facts, get_session, search_facts,
    search_facts_by_entity, search_facts_by_tags, search_facts_scored,
)
from kiso.sysenv import get_system_env, build_system_env_section

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
# Backward compat alias — callers migrated in M440/M446
TASK_TYPE_SKILL = TASK_TYPE_TOOL

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
_MAX_CONSOLIDATION_ITEMS = 200
_MAX_MESSENGER_FACTS = 50  # cap on facts injected into the messenger LLM context
_MESSENGER_RETRY_BACKOFF: float = 1.0  # M480: seconds between retries (0 in tests)
_MAX_MESSENGER_RETRIES = 2  # M480: max retries on transient LLM errors
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
# M615: marker substring in validation errors for uninstalled-tool detection.
# Used both when generating the error (validate_plan) and detecting it
# (_retry_llm_with_validation).  Keep in sync.
_TOOL_NOT_INSTALLED_MARKER = "is not installed"

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
            LLM retries (M308).  Only used once — after fallback, exhaustion
            raises normally.

    Returns:
        The validated parsed dict.
    """
    max_validation_retries = int(config.settings["max_validation_retries"])
    max_llm_retries = int(config.settings.get("max_llm_retries", 3))
    max_total = max_validation_retries + max_llm_retries

    last_errors: list[str] = []
    prev_error_set: frozenset[str] = frozenset()  # M186: track repeated identical errors
    repeat_count: int = 0
    llm_errors = 0
    validation_errors = 0
    attempt = 0
    active_model: str | None = None  # M308: None means use default from config
    saw_uninstalled_tool = False  # M615: track uninstalled-tool validation errors

    while attempt < max_total:
        attempt += 1

        if last_errors:
            error_lines = [f"- {e}" for e in last_errors]
            # M186: escalate after 2+ identical error patterns
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
            # M652: stall = provider-level issue — retry on same model is futile.
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
            # M630: circuit breaker open → switch to fallback immediately
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
                # M308: switch to fallback model instead of raising
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
                exc.last_errors = last_errors  # preserve for M195 auto-correction
                raise exc
            # M297: notify caller before retry
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
            # M615: propagate uninstalled-tool signal on the result dict
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

        # M615: detect uninstalled-tool errors for install-proposal detection
        if not saw_uninstalled_tool and any(_TOOL_NOT_INSTALLED_MARKER in e for e in errors):
            saw_uninstalled_tool = True

        # M186: track consecutive identical errors for escalation
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
}, ["goal", "secrets", "tasks", "extend_replan", "needs_install"])


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
    "context": {"type": "string"},
    "output_indices": {"type": "array", "items": {"type": "integer"}},
    "relevant_tags": {"type": "array", "items": {"type": "string"}},
    "relevant_entities": {"type": "array", "items": {"type": "string"}},
}, ["modules", "tools", "context", "output_indices", "relevant_tags", "relevant_entities"])

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
    "web", "replan", "scripting", "tool_recovery", "data_flow",
    "kiso_commands", "user_mgmt", "plugin_install",
})
_BRIEFER_MODULE_DESCRIPTIONS: dict[str, str] = {
    "planning_rules": "task ordering, expect rules, multi-step plans",
    "kiso_native": "kiso-first policy, registry checking",
    "tools_rules": "tool usage rules, atomic operations",
    "web": "URLs, websites, browser tool rules",
    "data_flow": "file-based data flow for large outputs",
    "scripting": "script execution rules",
    "replan": "replan strategy, extend_replan",
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


def _is_non_actionable_exec(detail: str) -> bool:
    """Return True if exec detail is analytical rather than shell-actionable."""
    lower = detail.lower().strip()
    if not any(lower.startswith(p) for p in _NON_ACTIONABLE_PREFIXES):
        return False
    # Allow if detail contains a concrete path or known binary
    if "/" in detail:
        return False
    return True


def _validate_plan_tasks(
    tasks: list[dict],
    installed_skills: list[str] | None,
    installed_skills_info: dict[str, dict] | None,
    install_approved: bool = False,
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
        if t == TASK_TYPE_EXEC and "pip install" in detail.lower() and "uv pip install" not in detail.lower():
            errors.append(
                f"Task {i}: use 'uv pip install' instead of 'pip install' — "
                f"uv is the project's package manager"
            )
        if t == TASK_TYPE_MSG:
            for field in ("expect", "tool", "args"):
                if task.get(field) is not None:
                    errors.append(f"Task {i}: msg task must have {field} = null")
            msg_detail = (task.get("detail") or "").strip()
            has_lang_prefix = bool(_ANSWER_IN_LANG_RE.match(msg_detail))
            if not has_lang_prefix:
                errors.append(
                    f"Task {i}: msg detail must start with 'Answer in {{language}}.' — "
                    f"always specify the response language, even for English"
                )
            else:
                cleaned = re.sub(r'^Answer in \w+\.\s*', '', msg_detail).strip()
                if len(cleaned) < 5:
                    errors.append(
                        f"Task {i}: msg detail is empty after language prefix — "
                        f"must contain WHAT to tell the user"
                    )
        if t == TASK_TYPE_SEARCH:
            if _is_plugin_discovery_search(task.get("detail", "")):
                errors.append(
                    f"Task {i}: search cannot be used for kiso plugin discovery — "
                    "use an exec task with `curl <registry_url>` instead"
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
            elif installed_skills is not None and tool_name not in installed_skills:
                available = ", ".join(sorted(installed_skills)) if installed_skills else "none"
                if install_approved:
                    errors.append(
                        f"Task {i}: tool '{tool_name}' is not installed. "
                        f"Available tools: {available}. "
                        f"You CANNOT use type=tool for uninstalled tools. "
                        f"Installation is approved — plan an exec task: "
                        f"`kiso tool install {tool_name}`, then replan to use it."
                    )
                else:
                    errors.append(
                        f"Task {i}: tool '{tool_name}' is not installed. "
                        f"Available tools: {available}. "
                        f"You CANNOT use '{tool_name}' in this plan. Remove the tool task. "
                        f"Plan a SINGLE msg task asking the user whether to install "
                        f"'{tool_name}', and offer alternatives (e.g. search instead of "
                        f"browser). End the plan with that msg — the user's reply triggers "
                        f"the next cycle."
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
                    if arg_errors:
                        example_args = {
                            aname: _TYPE_EXAMPLES.get(adef.get("type", "string"), "value")
                            for aname, adef in schema.items()
                        }
                        example_json = json.dumps(example_args)
                        errors.append(
                            f"Task {i}: tool '{tool_name}' args invalid: "
                            + "; ".join(arg_errors)
                            + f". Set args to a JSON string like: '{example_json}'"
                        )

    if replan_count > 1:
        errors.append("A plan can have at most one replan task")

    return errors


# M627: goal-plan mismatch — detect artifact requests with no exec/tool task.
_ARTIFACT_VERBS = frozenset({"create", "write", "generate", "build", "produce", "make"})
_ARTIFACT_NOUNS = frozenset({
    "file", "document", "script", "markdown", "csv", "report",
    "table", "spreadsheet", "config", "template", "page",
})


def _validate_plan_ordering(
    tasks: list[dict], is_replan: bool, install_approved: bool,
) -> list[str]:
    """Check cross-task ordering rules and install safety."""
    errors: list[str] = []

    # msg tasks must not appear before all data-gathering tasks.
    _DATA_TYPES = {TASK_TYPE_EXEC, TASK_TYPE_SEARCH, TASK_TYPE_TOOL}
    first_msg_idx = next((i for i, t in enumerate(tasks) if t.get("type") == TASK_TYPE_MSG), None)
    last_data_idx = next((i for i, t in reversed(list(enumerate(tasks))) if t.get("type") in _DATA_TYPES), None)
    if first_msg_idx is not None and last_data_idx is not None and first_msg_idx < last_data_idx:
        errors.append(
            f"Task {first_msg_idx + 1}: msg task must come after all "
            f"exec/search/tool tasks (task {last_data_idx + 1} is later). "
            f"Msg tasks communicate results — place them after investigation."
        )

    # M420/M428: install execs allowed in replans or when user approved in prior
    # msg cycle.  In a first plan without prior approval the planner must ask.
    if not is_replan and not install_approved:
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

    # M631: after installing a tool that was proposed in a prior turn, the
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


# M695: Types that can participate in parallel groups.
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
) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid).

    If installed_skills is provided, tool tasks are validated against it.
    If max_tasks is provided, plans with more tasks are rejected.
    If installed_skills_info is provided (name→tool dict), tool args are
    validated against the schema at plan time (M166).
    If is_replan is False, extend_replan is stripped (M171).
    """
    errors, tasks = _validate_plan_structure(plan, max_tasks, is_replan)
    if errors:
        return errors
    errors.extend(_validate_plan_tasks(
        tasks, installed_skills, installed_skills_info,
        install_approved=install_approved,
    ))
    errors.extend(_validate_plan_ordering(tasks, is_replan, install_approved))
    errors.extend(_validate_plan_groups(tasks))

    # M627: goal mentions creating a file/artifact but plan has no exec/tool task
    goal_words = set(plan.get("goal", "").lower().split())
    has_verb = bool(goal_words & _ARTIFACT_VERBS)
    has_noun = bool(goal_words & _ARTIFACT_NOUNS)
    has_action_task = any(
        t.get("type") in (TASK_TYPE_EXEC, TASK_TYPE_TOOL) for t in tasks
    )
    if has_verb and has_noun and not has_action_task:
        errors.append(
            "Goal mentions creating a file/document but plan has no exec or tool task. "
            "Add an exec task to write the file to the workspace — "
            "auto-publish will generate a download URL automatically."
        )

    # M640: coherence — tools listed in needs_install must not appear in tool tasks
    needs = plan.get("needs_install") or []
    if needs:
        for i, t in enumerate(tasks, 1):
            if t.get("type") == TASK_TYPE_TOOL and t.get("tool") in needs:
                errors.append(
                    f"Task {i}: tool '{t['tool']}' is in needs_install (not available) "
                    f"but used as a tool task. Remove the tool task or remove it from needs_install."
                )

    return errors


_FACT_CHAR_LIMIT = 200


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
) -> tuple[str, list, list, list, dict, str]:
    """Gather all raw context pieces for the planner.

    Returns (summary, facts, pending, recent, context_pool, sys_env_text).
    The context_pool dict is suitable for the briefer.
    """
    is_admin = user_role == "admin"
    context_limit = int(config.settings["context_messages"])

    # M545: batch independent DB queries
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
        primary, other = _split_facts_by_session(facts, session, is_admin)
        parts: list[str] = []
        if primary:
            grouped = _group_facts_by_category(primary)
            if grouped:
                parts.extend(grouped)
        if other:
            grouped = _group_facts_by_category(other, label_session=True)
            if grouped:
                parts.append("### From Other Sessions")
                parts.extend(grouped)
        facts_text = "\n".join(parts)

    pending_text = _format_pending_items(pending)
    recent_text = _format_message_history(recent)

    sys_env = get_system_env(config)
    sys_env_text = build_system_env_section(sys_env, session=session)

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

    # sys_env_text always present — semi-static
    context_pool["system_env"] = sys_env_text

    # M449/M450: inject MD-based skills into context pool
    md_skills = discover_md_skills()
    md_skills_text = build_planner_skill_list(md_skills)
    if md_skills_text:
        context_pool["md_skills"] = md_skills_text

    # M346: inject available entities for briefer selection
    all_entities = await get_all_entities(db)
    if all_entities:
        context_pool["available_entities"] = "\n".join(
            f"{e['name']} ({e['kind']})" for e in all_entities
        )

    return summary, facts, pending, recent, context_pool, sys_env_text


async def build_planner_messages(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_tools: str | list[str] | None = None,
    paraphrased_context: str | None = None,
    is_replan: bool = False,
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
    summary, facts, pending, recent, context_pool, sys_env_text = \
        await _gather_planner_context(
            db, config, session, user_role, new_message, paraphrased_context,
        )

    # M309: system env doesn't change between plan and replan — exclude from
    # briefer context pool to reduce redundant tokens.
    if is_replan:
        context_pool.pop("system_env", None)

    # Tool discovery — rescan on each planner call
    installed = discover_tools()
    installed_names = [s["name"] for s in installed]

    # Build the tool list text for context pool
    full_tool_list = build_planner_tool_list(installed, user_role, user_tools)
    if full_tool_list:
        context_pool["tools"] = full_tool_list

    msg_lower = new_message.lower()

    # --- Registry: show available-but-not-installed tools ---
    # Only fetch when no tools are installed.  Skip on replans — registry
    # data is identical to the initial plan and tools won't change mid-replan.
    registry_text = ""
    if not installed and not is_replan:
        registry_text = await asyncio.to_thread(
            get_registry_tools, set(installed_names),
        )
        if registry_text:
            context_pool["available_registry_tools"] = registry_text

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

    if briefing:
        # Briefer path: modules selected by the briefer LLM.
        # Safety net: force plugin_install when tools need installing.
        modules = list(briefing["modules"])
        if not installed or registry_text:
            if "plugin_install" not in modules:
                modules.append("plugin_install")
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
        system_prompt = _load_modular_prompt("planner", fallback_modules)

    if not installed:
        log.warning("discover_tools() returned empty — no tools available for planner")

    is_admin = user_role == "admin"

    # --- M390: Scored fact retrieval (briefer path only) ---
    scored_facts_text = ""
    if briefing:
        entity_id = None
        if briefing.get("relevant_entities"):
            all_entities = await get_all_entities(db)
            entity_map = {_normalize_entity_name(e["name"]): e["id"] for e in all_entities}
            # M552: filter out hallucinated entity names
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
    else:
        # Fallback path: full context dump (original behavior)
        _add_section(context_parts, "Session Summary", summary)

        if facts:
            primary, other = _split_facts_by_session(facts, session, is_admin)

            if primary:
                parts = _group_facts_by_category(primary)
                if parts:
                    context_parts.append("## Known Facts\n" + "\n".join(parts))

            if other:
                parts = _group_facts_by_category(other, label_session=True)
                if parts:
                    context_parts.append("## Context from Other Sessions\n" + "\n".join(parts))

        # M522/M551: entity-based fact enrichment (parity with briefer path)
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
        context_parts.append(f"## System Environment\n{sys_env_text}")

        _add_section(context_parts, "Pending Questions", _format_pending_items(pending))

        if recent:
            context_parts.append(
                f"## Recent Messages\n{fence_content(_format_message_history(recent), 'MESSAGES')}"
            )

        if paraphrased_context:
            context_parts.append(
                f"## Paraphrased External Messages (untrusted)\n"
                f"{fence_content(paraphrased_context, 'PARAPHRASED')}"
            )

    # MD skills section — briefer includes via context_pool; fallback path adds directly
    if not briefing and context_pool.get("md_skills"):
        context_parts.append(f"## Available Skills\n{context_pool['md_skills']}")

    # Tools section — briefer filters or full list
    if briefing and briefing["tools"]:
        context_parts.append(f"## Tools\n" + "\n".join(briefing["tools"]))
    elif full_tool_list:
        context_parts.append(f"## Tools\n{full_tool_list}")

    # M266/M544: warn planner when web module is active but browser isn't installed.
    # Emphasise that built-in search works without any tool for research queries.
    if "web" in (modules if briefing else fallback_modules) and "browser" not in installed_names:
        context_parts.append(
            "## Browser Availability\n"
            "The browser tool is NOT installed. "
            "For web research and reading page content, use the built-in `search` task type — "
            "it requires no tool and works immediately. "
            "The browser tool is only needed for interactive browsing (navigate to a specific URL, "
            "click, fill forms, take screenshots). "
            "If interactive browsing is required: single msg asking to install, end plan."
        )

    # M411: always-inject safety facts (not gated by briefer)
    safety_facts = await get_safety_facts(db)
    _add_section(context_parts, "Safety Rules (MUST OBEY)",
                 _join_or_empty(safety_facts, lambda f: f"- {f['content']}"))

    # M675: always-inject behavior facts (soft guidelines, not hard constraints)
    behavior_facts = await get_behavior_facts(db)
    _add_section(context_parts, "Behavior Guidelines (follow these preferences)",
                 _join_or_empty(behavior_facts, lambda f: f"- {f['content']}"))

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
            before each retry attempt (M297).
        max_tasks_override: M698 — override max_plan_tasks (used by replan
            shrinking to reduce the limit at deeper replan depths).

    Returns the validated plan dict with keys: goal, secrets, tasks.
    Raises PlanError if all retries exhausted.
    """
    messages, installed_names, installed_info = await build_planner_messages(
        db, config, session, user_role, new_message, user_tools=user_tools,
        paraphrased_context=paraphrased_context, is_replan=is_replan,
    )
    if on_context_ready:
        await on_context_ready()
    tools_by_name = {s["name"]: s for s in installed_info}

    max_tasks = max_tasks_override if max_tasks_override is not None else int(config.settings["max_plan_tasks"])

    # M698: inject task budget into planner context so LLM knows the limit.
    budget_line = f"\n\n## Task Budget\nMaximum tasks: {max_tasks}."
    for msg in reversed(messages):
        if msg["role"] == "user":
            msg["content"] += budget_line
            break
    else:
        log.warning("No user message found for budget injection")

    fallback = config.settings.get("planner_fallback_model") or None
    plan = await _retry_llm_with_validation(
        config, "planner", messages, PLAN_SCHEMA,
        lambda p: validate_plan(p, installed_skills=installed_names, max_tasks=max_tasks,
                                installed_skills_info=tools_by_name, is_replan=is_replan,
                                install_approved=install_approved),
        PlanError, "Plan",
        session=session,
        on_retry=on_retry,
        fallback_model=fallback,
    )
    # M640/M670/M711: detect install proposal from three sources:
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
    ("md_skills", "Available Skills (planner instructions)"),
    ("connectors", "Available Connectors"),
    ("system_env", "System Environment"),
    ("summary", "Session Summary"),
    ("facts", "Known Facts"),
    ("recent_messages", "Recent Messages"),
    ("pending", "Pending Questions"),
    ("available_tags", "Available Fact Tags"),
    ("available_entities", "Available Entities"),
    ("paraphrased", "Paraphrased External Messages"),
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
    # md_skills only relevant when skills are installed
    if not pool.get("md_skills"):
        pool.pop("md_skills", None)
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

    # M272: messenger/worker never use modules or skills — omit those sections
    # to save ~400 tokens per briefer call for these simple consumers.
    _simple_consumer = consumer_role in ("messenger", "worker")

    parts: list[str] = [
        f"## Consumer Role\n{consumer_role}",
        f"## Task\n{task_description}",
    ]
    if not _simple_consumer:
        parts.append(f"## Available Modules\n{_BRIEFER_MODULES_STR}")

    # M272: skip sections irrelevant for simple consumers
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
    if not isinstance(briefing.get("context"), str):
        errors.append("context must be a string")
    if not isinstance(briefing.get("output_indices"), list):
        errors.append("output_indices must be an array")
    if not isinstance(briefing.get("relevant_tags"), list):
        errors.append("relevant_tags must be an array")
    if not isinstance(briefing.get("relevant_entities"), list):
        errors.append("relevant_entities must be an array")
    return errors


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

    # M304: simple consumers never use modules — skip module name validation
    # to avoid wasted retries when the model hallucinates names.
    _simple = consumer_role in ("messenger", "worker")
    vfn = (lambda b: validate_briefing(b, check_modules=False)) if _simple else validate_briefing

    briefing = await _retry_llm_with_validation(
        config, "briefer", messages, BRIEFER_SCHEMA,
        vfn,
        BrieferError, "Briefing",
        session=session,
    )

    # M304: force modules=[] for simple consumers (defensive cleanup)
    if _simple:
        briefing["modules"] = []

    # M368/M387: post-validation filtering — remove hallucinated tools
    if briefing["tools"]:
        if not context_pool.get("tools"):
            # No tools installed — any briefer tool selection is hallucinated
            log.debug("Briefer: cleared %d hallucinated tool(s) (none installed)",
                      len(briefing["tools"]))
            briefing["tools"] = []
        else:
            # M394: extract installed tool names for exact matching
            installed_tool_names: set[str] = set()
            for line in context_pool["tools"].split("\n"):
                m = re.match(r"^-\s+(\S+)", line)
                if m:
                    installed_tool_names.add(m.group(1).lower())
            original_count = len(briefing["tools"])
            briefing["tools"] = [
                s for s in briefing["tools"]
                if s.split(":")[0].split()[0].strip().lower() in installed_tool_names
            ]
            filtered = original_count - len(briefing["tools"])
            if filtered:
                log.debug("Briefer: filtered %d hallucinated tool(s)", filtered)

    log.info(
        "Briefer for %s: %d modules, %d tools, %d output_indices, %d tags",
        consumer_role,
        len(briefing["modules"]),
        len(briefing["tools"]),
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
) -> list[dict]:
    """Build the message list for the classifier LLM call."""
    user_text = content
    if recent_context:
        user_text = f"{content}\n\n## Recent Context\n{recent_context}"
    return _build_messages(_load_system_prompt("classifier"), user_text)


CLASSIFIER_CATEGORIES: frozenset[str] = frozenset({"plan", "chat", "chat_kb"})


async def classify_message(
    config: Config, content: str, session: str = "",
    recent_context: str = "",
) -> tuple[str, str]:
    """Classify a user message and detect its language.

    Returns ``(category, lang)`` where *category* is one of
    :data:`CLASSIFIER_CATEGORIES` and *lang* is an ISO 639-1 code
    (e.g. ``"en"``, ``"it"``).  On any error or ambiguous output,
    returns ``("plan", "en")`` as safe fallback.
    """
    messages = build_classifier_messages(content, recent_context=recent_context)
    try:
        raw = await call_llm(config, "classifier", messages, session=session)
    except LLMError as e:
        log.warning("Classifier LLM failed, falling back to plan: %s", e)
        return "plan", "en"

    result = raw.strip().lower()

    # Expected format: "cat:lang" (e.g. "chat:it", "plan:en")
    if ":" in result:
        cat, lang = result.split(":", 1)
        cat = cat.strip()
        lang = lang.strip()
        if cat in CLASSIFIER_CATEGORIES and len(lang) == 2:
            log.info("Classifier: %s (lang=%s)", cat, lang)
            return cat, lang
        # Defensive: LLM returned literal "category:xx" or "category:xx:cat"
        if cat == "category" and len(lang) == 2:
            log.info("Classifier: plan (literal 'category', lang=%s)", lang)
            return "plan", lang
        if cat == "category" and ":" in lang:
            lang_part, cat_part = lang.split(":", 1)
            if cat_part.strip() in CLASSIFIER_CATEGORIES and len(lang_part.strip()) == 2:
                log.info("Classifier: %s (literal 'category', lang=%s)", cat_part.strip(), lang_part.strip())
                return cat_part.strip(), lang_part.strip()

    # Backward compat: plain category without lang
    if result in CLASSIFIER_CATEGORIES:
        log.info("Classifier: %s (no lang)", result)
        return result, "en"

    # Ambiguous output — safe fallback
    log.warning("Classifier returned unexpected value %r, falling back to plan", raw.strip())
    return "plan", "en"


# --- Stop pattern fast-path (M407) ---

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


# --- In-flight message classification (M406) ---


def build_inflight_classifier_messages(
    plan_goal: str, new_message: str,
) -> list[dict]:
    """Build the message list for in-flight message classification."""
    template = _load_system_prompt("inflight-classifier")
    user_text = template.replace(
        "{plan_goal}", plan_goal,
    ).replace("{new_message}", new_message)
    return [{"role": "user", "content": user_text}]


async def classify_inflight(
    config: Config, plan_goal: str, new_message: str,
    session: str = "",
) -> str:
    """Classify an in-flight message as stop/update/independent/conflict.

    Returns one of :data:`INFLIGHT_CATEGORIES`. On any error, returns
    ``"independent"`` (safe fallback — message will be queued for later).
    """
    messages = build_inflight_classifier_messages(plan_goal, new_message)
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


# --- Learning quality filters (M320) ---

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
    - Negative claims contradicted by task output (M373)
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


_REVIEWER_OUTPUT_LIMIT = 4000
_REVIEWER_STDERR_BUDGET = 1500
_REVIEWER_STDERR_MAX_LINES = 40
_REVIEWER_TAIL_LINES = 80
_REVIEWER_MAX_GREP_MATCHES = 20

_ERROR_RE = re.compile(
    r"error|fail|exception|traceback|warning|denied|not found|fatal|panic|refused|timeout|errno",
    re.IGNORECASE,
)


def prepare_reviewer_output(
    stdout: str, stderr: str, limit: int = _REVIEWER_OUTPUT_LIMIT,
) -> str:
    """Prepare task output for the reviewer LLM.

    For small outputs, returns the combined text as-is.  For large outputs,
    builds a compact digest: stderr (priority) + error grep matches from
    stdout + tail of stdout, all within *limit* chars.
    """
    combined = stdout
    if stderr:
        combined += f"\n--- stderr ---\n{stderr}"
    if len(combined) <= limit:
        return combined

    original_len = len(stdout) + len(stderr)
    parts: list[str] = []

    # 1. Stderr section (priority — errors live here)
    stderr_section = ""
    stderr_budget = _REVIEWER_STDERR_BUDGET
    if stderr.strip():
        stderr_lines = stderr.splitlines()[:_REVIEWER_STDERR_MAX_LINES]
        stderr_section = "\n".join(stderr_lines)
        if len(stderr_section) > stderr_budget:
            stderr_section = stderr_section[:stderr_budget] + "\n... (stderr truncated)"
        parts.append(f"--- stderr ({len(stderr_lines)} lines) ---\n{stderr_section}")

    # 2. Stdout tail (last N lines — most valuable)
    stdout_lines = stdout.splitlines()
    tail_lines = stdout_lines[-_REVIEWER_TAIL_LINES:]
    tail_text = "\n".join(tail_lines)

    # 3. Error grep — scan FULL stdout, collect unique matches not in tail
    tail_set = set(tail_lines)
    grep_matches: list[str] = []
    for i, line in enumerate(stdout_lines):
        if _ERROR_RE.search(line) and line not in tail_set:
            # Include 1 line of context before
            context_line = stdout_lines[i - 1] if i > 0 else ""
            entry = f"{context_line}\n{line}".strip() if context_line else line
            if entry not in grep_matches:
                grep_matches.append(entry)
            if len(grep_matches) >= _REVIEWER_MAX_GREP_MATCHES:
                break

    if grep_matches:
        grep_text = "\n".join(grep_matches)
        parts.append(f"--- error matches (from full stdout) ---\n{grep_text}")

    parts.append(f"--- last {len(tail_lines)} lines of stdout ---\n{tail_text}")

    # Assemble and apply hard cap
    header = f"[OUTPUT TRUNCATED — original {original_len} chars, showing"
    if stderr_section:
        header += " stderr +"
    if grep_matches:
        header += " error matches +"
    header += f" last {len(tail_lines)} lines]\n"

    body = "\n".join(parts)

    # Shrink to fit: trim tail first, then grep, then stderr
    result = header + body
    if len(result) > limit:
        # Recalculate with trimmed tail
        available = limit - len(header) - sum(len(p) + 1 for p in parts[:-1])
        if available > 100:
            tail_text = tail_text[:available] + "\n... (tail trimmed)"
            parts[-1] = f"--- last {len(tail_lines)} lines of stdout ---\n{tail_text}"
        body = "\n".join(parts)
        result = header + body

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

    # M412: inject safety rules for compliance check
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
    log.info("Review: status=%s", review["status"])
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
        # M343: entity required for promote
        if verdict == CURATOR_VERDICT_PROMOTE:
            if not ev.get("entity_name"):
                errors.append(f"Evaluation {i}: promoted fact must have entity_name")
            kind = ev.get("entity_kind")
            if not kind or kind not in _ENTITY_KINDS:
                errors.append(f"Evaluation {i}: promoted fact must have valid entity_kind")
    return errors


def _select_curator_modules(
    learnings: list[dict],
    available_tags: list[str] | None,
    available_entities: list[dict] | None,
) -> list[str]:
    """Heuristic module selection for curator.

    Always includes ``entity_assignment`` (needed for any promote).
    Includes ``tag_reuse`` only when existing tags are available
    (otherwise the "check existing tags" instruction is vacuous).
    """
    modules: list[str] = ["entity_assignment"]
    if available_tags:
        modules.append("tag_reuse")
    return modules


def build_curator_messages(
    learnings: list[dict],
    available_tags: list[str] | None = None,
    available_entities: list[dict] | None = None,
    existing_facts: list[dict] | None = None,
) -> list[dict]:
    """Build the message list for the curator LLM call."""
    modules = _select_curator_modules(learnings, available_tags, available_entities)
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
    parts.append(f"## Messages\n{_format_message_history(messages)}")
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
    system_prompt = _load_system_prompt("messenger").replace("{bot_name}", bot_name)

    context_parts: list[str] = []
    # M502: extract language from "Answer in {lang}." prefix and inject as
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
            f"## Recent Conversation\n{fence_content(_format_message_history(recent_messages), 'MESSAGES')}"
        )
    # M675: inject behavioral guidelines
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
    # M675: fetch behavior guidelines for messenger
    behavior_facts = await get_behavior_facts(db)
    behavior_rules = [f["content"] for f in behavior_facts] if behavior_facts else None
    messages = build_messenger_messages(
        config, summary, facts, detail, plan_outputs_text, goal=goal,
        recent_messages=recent or None, user_message=user_message,
        briefing_context=briefing_context, behavior_rules=behavior_rules,
    )
    # M480: retry messenger LLM call up to 2 times on transient errors.
    # M666: on SSE stall, switch to fallback model immediately (don't waste retries).
    _fallback = config.settings.get("planner_fallback_model", "google/gemini-2.5-flash-lite")
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


# M369: strip hallucinated XML/tool markup from messenger output
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
) -> list[dict]:
    """Build the message list for the exec translator LLM call."""
    system_prompt = _load_system_prompt("worker")
    context_parts: list[str] = [f"## System Environment\n{sys_env_text}"]
    _add_section(context_parts, "Preceding Task Outputs", plan_outputs_text)
    _add_section(context_parts, "Retry Context", retry_context)
    context_parts.append(f"## Task\n{detail}")
    return _build_messages(system_prompt, "\n\n".join(context_parts))


async def run_exec_translator(
    config: Config,
    detail: str,
    sys_env_text: str,
    plan_outputs_text: str = "",
    session: str = "",
    retry_context: str = "",
) -> str:
    """Translate a natural-language exec task detail into a shell command.

    Returns the shell command string.
    Raises ExecTranslatorError on failure.
    """
    messages = build_exec_translator_messages(
        config, detail, sys_env_text, plan_outputs_text,
        retry_context=retry_context,
    )
    _fallback = config.settings.get("planner_fallback_model", "google/gemini-2.5-flash-lite")
    raw = await _call_role(config, "worker", messages, ExecTranslatorError, session,
                           fallback_model=_fallback)

    command = raw.strip()
    if not command or command == "CANNOT_TRANSLATE":
        raise ExecTranslatorError(
            f"Cannot translate task to shell command: {detail}"
        )
    # M504: syntax-check long commands before execution
    if len(command) > 120:
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
                raise ExecTranslatorError(
                    f"Bash syntax error in generated command: {hint}"
                )
        except FileNotFoundError:
            pass  # bash not available — skip check
    return command


# ---------------------------------------------------------------------------
# Fact consolidation
# ---------------------------------------------------------------------------

async def run_fact_consolidation(
    config: Config, facts: list[dict], session: str = "",
) -> list[dict]:
    """Consolidate/deduplicate facts via LLM. Returns list of consolidated fact dicts.

    Each dict has keys: content (str), category (str), confidence (float).
    Plain strings from backward-compatible LLM responses are wrapped into dicts.

    Raises SummarizerError on failure.
    """
    system_prompt = _load_system_prompt("summarizer-facts")
    facts_text = _join_or_empty(facts, lambda f: f"- {f['content']}")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"## Facts\n{facts_text}"},
    ]
    try:
        raw = await call_llm(config, "summarizer", messages, session=session)
    except LLMError as e:
        raise SummarizerError(f"LLM call failed: {e}")
    try:
        result = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        raise SummarizerError(f"Consolidation returned invalid JSON: {e}")
    if not isinstance(result, list):
        raise SummarizerError("Consolidation must return a JSON array")

    if len(result) > _MAX_CONSOLIDATION_ITEMS:
        log.warning(
            "Consolidation returned %d items (cap %d), truncating",
            len(result), _MAX_CONSOLIDATION_ITEMS,
        )
        result = result[:_MAX_CONSOLIDATION_ITEMS]

    # Normalize items: dicts with content key, or plain strings (backward compat)
    normalized: list[dict] = []
    for item in result:
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            content = item["content"].strip()
            if len(content) < 3:
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 1.0))))
            except (ValueError, TypeError):
                confidence = 1.0
            raw_category = item.get("category") or "general"
            category = raw_category if raw_category in _VALID_FACT_CATEGORIES else "general"
            normalized.append({
                "content": content,
                "category": category,
                "confidence": confidence,
            })
        elif isinstance(item, str):
            content = item.strip()
            if len(content) < 3:
                continue
            normalized.append({
                "content": content,
                "category": "general",
                "confidence": 1.0,
            })
        # Skip invalid items
    return normalized
