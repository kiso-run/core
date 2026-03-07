"""Planner brain — builds context, calls LLM, validates plan."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import aiosqlite

from kiso.config import Config, KISO_DIR
from kiso.llm import LLMError, call_llm
from kiso.security import fence_content
from kiso.skills import discover_skills, build_planner_skill_list, validate_skill_args
from kiso.store import get_facts, get_pending_items, get_recent_messages, get_session, search_facts
from kiso.sysenv import get_system_env, build_system_env_section

log = logging.getLogger(__name__)

# Task type constants
TASK_TYPE_EXEC = "exec"
TASK_TYPE_MSG = "msg"
TASK_TYPE_SKILL = "skill"
TASK_TYPE_SEARCH = "search"
TASK_TYPE_REPLAN = "replan"
TASK_TYPES: frozenset[str] = frozenset({
    TASK_TYPE_EXEC, TASK_TYPE_MSG, TASK_TYPE_SKILL, TASK_TYPE_SEARCH, TASK_TYPE_REPLAN,
})

# Review status constants
REVIEW_STATUS_OK = "ok"
REVIEW_STATUS_REPLAN = "replan"
REVIEW_STATUSES: frozenset[str] = frozenset({REVIEW_STATUS_OK, REVIEW_STATUS_REPLAN})

# Curator verdict constants
CURATOR_VERDICT_PROMOTE = "promote"
CURATOR_VERDICT_ASK = "ask"
CURATOR_VERDICT_DISCARD = "discard"
CURATOR_VERDICTS: frozenset[str] = frozenset({
    CURATOR_VERDICT_PROMOTE, CURATOR_VERDICT_ASK, CURATOR_VERDICT_DISCARD,
})

# Example values for skill args types (used in validation error messages)
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
_VALID_FACT_CATEGORIES: frozenset[str] = frozenset({"general", "project", "tool", "user"})


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


_PLUGIN_DISCOVERY_RE = re.compile(
    r"(?:skill|connector|plugin).*(?:registr|install|discover|find|search|browse|cercar)"
    r"|(?:registr|kiso).*(?:skill|connector|plugin)",
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
) -> dict:
    """Generic retry loop: call LLM, parse JSON, validate, retry on errors.

    Args:
        config: App config (reads max_validation_retries).
        role: LLM model route name (e.g. "planner", "reviewer", "curator").
        messages: Initial message list (mutated in-place with retries).
        schema: JSON schema for structured output.
        validate_fn: Callable(parsed_dict) → list[str] errors.
        error_class: Exception type to raise on exhaustion.
        error_noun: Human noun for error messages (e.g. "Plan", "Review").
        session: Session name for LLM call tracking.

    Returns:
        The validated parsed dict.
    """
    max_retries = int(config.settings["max_validation_retries"])
    last_errors: list[str] = []
    prev_error_set: frozenset[str] = frozenset()  # M186: track repeated identical errors
    repeat_count: int = 0

    for attempt in range(1, max_retries + 1):
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
            raw = await call_llm(config, role, messages, response_format=schema, session=session)
        except LLMError as e:
            raise error_class(f"LLM call failed: {e}")

        try:
            result = json.loads(_repair_json(raw))
        except json.JSONDecodeError as e:
            log.warning("%s returned invalid JSON (attempt %d/%d): %s",
                        error_noun, attempt, max_retries, e)
            last_errors = [
                f"Invalid JSON at line {e.lineno} col {e.colno}: {e.msg} — "
                "return ONLY the JSON object, no markdown, no trailing commas"
            ]
            messages.append({"role": "assistant", "content": raw})
            continue

        errors = validate_fn(result)
        if not errors:
            log.info("%s accepted (attempt %d)", error_noun, attempt)
            return result

        log.warning("%s validation failed (attempt %d/%d): %s",
                    error_noun, attempt, max_retries, errors)
        # M186: track consecutive identical errors for escalation
        error_set = frozenset(errors)
        if error_set == prev_error_set:
            repeat_count += 1
        else:
            prev_error_set = error_set
            repeat_count = 1
        last_errors = errors
        messages.append({"role": "assistant", "content": raw})

    exc = error_class(
        f"{error_noun} validation failed after {max_retries} attempts: {last_errors}"
    )
    exc.last_errors = last_errors  # M195: expose raw errors for auto-correction
    raise exc


PLAN_SCHEMA: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "plan",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "secrets": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                                "required": ["key", "value"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ],
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["exec", "msg", "skill", "search", "replan"],
                            },
                            "detail": {"type": "string"},
                            "skill": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "args": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "expect": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        },
                        "required": ["type", "detail", "skill", "args", "expect"],
                        "additionalProperties": False,
                    },
                },
                "extend_replan": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                },
            },
            "required": ["goal", "secrets", "tasks", "extend_replan"],
            "additionalProperties": False,
        },
    },
}


REVIEW_SCHEMA: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "review",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["ok", "replan"],
                },
                "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "learn": {"anyOf": [
                    {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    {"type": "null"},
                ]},
                "retry_hint": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["status", "reason", "learn", "retry_hint", "summary"],
            "additionalProperties": False,
        },
    },
}


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


def _build_messages(system_prompt: str, user_content: str) -> list[dict]:
    """Assemble the canonical [system, user] message pair used by all LLM roles."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def validate_plan(
    plan: dict,
    installed_skills: list[str] | None = None,
    max_tasks: int | None = None,
    installed_skills_info: dict[str, dict] | None = None,
    is_replan: bool = False,
) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid).

    If installed_skills is provided, skill tasks are validated against it.
    If max_tasks is provided, plans with more tasks are rejected.
    If installed_skills_info is provided (name→skill dict), skill args are
    validated against the schema at plan time (M166).
    If is_replan is False, extend_replan is stripped (M171).
    """
    # Strip extend_replan from initial plans (M171)
    if not is_replan:
        plan.pop("extend_replan", None)
    errors: list[str] = []
    tasks = plan.get("tasks", [])

    if not tasks:
        errors.append("tasks list must not be empty")
        return errors

    if max_tasks and len(tasks) > max_tasks:
        errors.append(f"Plan has {len(tasks)} tasks, max allowed is {max_tasks}")

    replan_count = 0
    for i, task in enumerate(tasks, 1):
        t = task.get("type")
        if t not in TASK_TYPES:
            errors.append(f"Task {i}: unknown type {t!r}")
            continue
        if t in (TASK_TYPE_EXEC, TASK_TYPE_SKILL, TASK_TYPE_SEARCH) and task.get("expect") is None:
            errors.append(f"Task {i}: {t} task must have a non-null expect")
        detail = task.get("detail") or ""
        if t == TASK_TYPE_EXEC and len(detail) > 500:
            errors.append(
                f"Task {i}: exec detail is {len(detail)} chars — too long. "
                f"Detail must be natural language intent, not embedded data or commands. "
                f"Save large data to files and reference the file path instead."
            )
        if t == TASK_TYPE_MSG:
            for field in ("expect", "skill", "args"):
                if task.get(field) is not None:
                    errors.append(f"Task {i}: msg task must have {field} = null")
        if t == TASK_TYPE_SEARCH:
            if _is_plugin_discovery_search(task.get("detail", "")):
                errors.append(
                    f"Task {i}: search cannot be used for kiso plugin discovery — "
                    "use an exec task with `curl <registry_url>` instead"
                )
            if task.get("skill") is not None:
                errors.append(f"Task {i}: search task must have skill = null")
        if t == TASK_TYPE_REPLAN:
            replan_count += 1
            if task.get("expect") is not None:
                errors.append(f"Task {i}: replan task must have expect = null")
            if task.get("skill") is not None:
                errors.append(f"Task {i}: replan task must have skill = null")
            if task.get("args") is not None:
                errors.append(f"Task {i}: replan task must have args = null")
            if i != len(tasks):
                errors.append(f"Task {i}: replan task can only be the last task")
        if t == TASK_TYPE_SKILL:
            skill_name = task.get("skill")
            if not skill_name:
                errors.append(f"Task {i}: skill task must have a non-null skill name")
            elif installed_skills is not None and skill_name not in installed_skills:
                available = ", ".join(sorted(installed_skills)) if installed_skills else "none"
                errors.append(
                    f"Task {i}: skill '{skill_name}' is not installed. "
                    f"Available skills: {available}. "
                    f"You CANNOT use '{skill_name}' in this plan. Remove the skill task. "
                    f"Correct structure: "
                    f'[{{"type": "exec", "detail": "Install the {skill_name} skill using '
                    f'kiso skill install {skill_name}", "expect": "install succeeds", ...}}, '
                    f'{{"type": "replan", "detail": "Use {skill_name} after install", ...}}]'
                )
            elif installed_skills_info and skill_name in installed_skills_info:
                # Validate args against schema (M166)
                args_raw = task.get("args") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except (json.JSONDecodeError, TypeError):
                    errors.append(f"Task {i}: skill args is not valid JSON")
                else:
                    schema = installed_skills_info[skill_name].get("args_schema", {})
                    arg_errors = validate_skill_args(args, schema)
                    if arg_errors:
                        # M184: include example args format
                        example_args = {
                            aname: _TYPE_EXAMPLES.get(adef.get("type", "string"), "value")
                            for aname, adef in schema.items()
                        }
                        example_json = json.dumps(example_args)
                        errors.append(
                            f"Task {i}: skill '{skill_name}' args invalid: "
                            + "; ".join(arg_errors)
                            + f". Set args to a JSON string like: '{example_json}'"
                        )

    if replan_count > 1:
        errors.append("A plan can have at most one replan task")

    # msg tasks must not appear before all data-gathering tasks.
    # Find the index of the first msg and the last exec/search/skill.
    _DATA_TYPES = {TASK_TYPE_EXEC, TASK_TYPE_SEARCH, TASK_TYPE_SKILL}
    first_msg_idx = next((i for i, t in enumerate(tasks) if t.get("type") == TASK_TYPE_MSG), None)
    last_data_idx = next((i for i, t in reversed(list(enumerate(tasks))) if t.get("type") in _DATA_TYPES), None)
    if first_msg_idx is not None and last_data_idx is not None and first_msg_idx < last_data_idx:
        errors.append(
            f"Task {first_msg_idx + 1}: msg task must come after all "
            f"exec/search/skill tasks (task {last_data_idx + 1} is later). "
            f"Msg tasks communicate results — place them after investigation."
        )

    last = tasks[-1]
    if last.get("type") not in (TASK_TYPE_MSG, TASK_TYPE_REPLAN):
        errors.append("Last task must be type 'msg' or 'replan'")

    return errors


_FACT_CHAR_LIMIT = 200


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


# Capability keywords → skill name they require.  Used by the
# capability-gap heuristic to inject plugin-install guidance when the
# message implies a capability not covered by installed skills.
_CAPABILITY_MAP: dict[str, str] = {
    "screenshot": "browser",
    "schermata": "browser",
    "navigate": "browser",
    "naviga": "browser",
    "click": "browser",
    "clicca": "browser",
    "form": "browser",
    "browse": "browser",
    "webpage": "browser",
    "refactor": "aider",
    "debug": "aider",
}


def _detect_capability_gap(msg_lower: str, installed_names: set[str]) -> str | None:
    """Return the missing skill name if the message implies an uninstalled capability."""
    words = set(msg_lower.split())
    for keyword, skill in _CAPABILITY_MAP.items():
        if keyword in words and skill not in installed_names:
            return skill
    return None


async def build_planner_messages(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_skills: str | list[str] | None = None,
    paraphrased_context: str | None = None,
) -> tuple[list[dict], list[str], list[dict]]:
    """Build the message list for the planner LLM call.

    Assembles context from session summary, facts, pending questions,
    system environment, skills, and recent messages.

    The *session* name is passed to ``build_system_env_section`` so the
    planner sees the absolute workspace path (``KISO_DIR/sessions/<session>``)
    and a ``Session:`` line — giving it precise knowledge of the execution
    directory for shell commands.

    Returns (messages, installed_skill_names, installed_skills_info) — the
    caller can reuse the skill names list for plan validation and the
    skills_info list for args validation without rescanning the filesystem.
    """
    system_prompt = _load_system_prompt("planner")

    # Contextual appendix blocks — injected only when the message touches
    # the relevant topic.  False positives are harmless (extra guidance);
    # false negatives degrade gracefully (planner lacks detail, may ask).
    msg_lower = new_message.lower()
    appendix_parts: list[str] = []

    _kiso_kw = {"skill", "connector", "env", "instance", "kiso"}
    if _kiso_kw & set(msg_lower.split()):
        appendix_parts.append(_load_system_prompt("planner-kiso-commands"))

    _user_kw = {"user", "utente", "admin", "alias"}
    if _user_kw & set(msg_lower.split()):
        appendix_parts.append(_load_system_prompt("planner-user-mgmt"))

    _plugin_kw = {"install", "installa", "plugin", "add"}
    _plugin_install_needed = (
        _plugin_kw & set(msg_lower.split())
        or "not installed" in msg_lower
        or "registry" in msg_lower
    )
    if _plugin_install_needed:
        appendix_parts.append(_load_system_prompt("planner-plugin-install"))

    if appendix_parts:
        system_prompt = system_prompt.rstrip() + "\n\n" + "\n\n".join(appendix_parts)

    # Context pieces
    sess = await get_session(db, session)
    summary = sess["summary"] if sess else ""
    is_admin = user_role == "admin"
    facts = await search_facts(db, new_message, session=session, is_admin=is_admin)
    pending = await get_pending_items(db, session)
    context_limit = int(config.settings["context_messages"])
    recent = await get_recent_messages(db, session, limit=context_limit)

    # Build context block
    context_parts: list[str] = []

    if summary:
        context_parts.append(f"## Session Summary\n{summary}")

    if facts:
        # For admin: split current-session+global facts from other-session facts so
        # the planner sees a clear priority hierarchy — current session is primary,
        # other sessions are background context.
        if is_admin:
            primary = [f for f in facts if not f.get("session") or f.get("session") == session]
            other   = [f for f in facts if f.get("session") and f.get("session") != session]
        else:
            primary = facts
            other   = []

        if primary:
            parts = _group_facts_by_category(primary)
            if parts:
                context_parts.append("## Known Facts\n" + "\n".join(parts))

        if other:
            parts = _group_facts_by_category(other, label_session=True)
            if parts:
                context_parts.append("## Context from Other Sessions\n" + "\n".join(parts))

    # System environment — semi-static context about the execution environment
    sys_env = get_system_env(config)
    sys_env_text = build_system_env_section(sys_env, session=session)
    context_parts.append(f"## System Environment\n{sys_env_text}")

    if pending:
        pending_text = "\n".join(f"- {p['content']}" for p in pending)
        context_parts.append(f"## Pending Questions\n{pending_text}")

    if recent:
        msgs_text = "\n".join(
            f"[{m['role']}] {m['user'] or 'system'}: {m['content']}"
            for m in recent
        )
        context_parts.append(f"## Recent Messages\n{fence_content(msgs_text, 'MESSAGES')}")

    if paraphrased_context:
        context_parts.append(
            f"## Paraphrased External Messages (untrusted)\n"
            f"{fence_content(paraphrased_context, 'PARAPHRASED')}"
        )

    # Skill discovery — rescan on each planner call
    installed = discover_skills()
    if not installed:
        log.warning("discover_skills() returned empty — no skills available for planner")
        if not _plugin_install_needed:
            system_prompt = system_prompt.rstrip() + "\n\n" + _load_system_prompt("planner-plugin-install")
            _plugin_install_needed = True
    installed_names = [s["name"] for s in installed]

    # Capability-gap heuristic: if the message implies a capability that
    # no installed skill provides, inject the plugin-install appendix so
    # the planner knows how to discover and install the missing skill.
    _gap = _detect_capability_gap(msg_lower, set(installed_names))
    if _gap:
        log.info("Capability gap detected: message needs %r but not installed", _gap)
        if not _plugin_install_needed:
            system_prompt = system_prompt.rstrip() + "\n\n" + _load_system_prompt("planner-plugin-install")
            _plugin_install_needed = True
        # M198: expose the detected skill name to the planner
        context_parts.append(
            f"## Capability Analysis\n"
            f"Skill '{_gap}' is needed for this request but not installed. "
            f"Install it with: exec `kiso skill install {_gap}`, then replan."
        )
    skill_list = build_planner_skill_list(installed, user_role, user_skills)
    if skill_list:
        context_parts.append(f"## Skills\n{skill_list}")

    context_parts.append(f"## Caller Role\n{user_role}")
    context_parts.append(f"## New Message\n{fence_content(new_message, 'USER_MSG')}")

    context_block = "\n\n".join(context_parts)

    return _build_messages(system_prompt, context_block), installed_names, installed


_SKILL_NOT_INSTALLED_RE = re.compile(r"skill '([^']+)' is not installed")


def _auto_correct_uninstalled_skills(errors: list[str], messages: list[dict]) -> dict | None:
    """M195: Build a corrected plan when validation fails due to uninstalled skills.

    Extracts skill names from the raw validation error list, and replaces the plan
    with exec install + replan.
    Returns None if no errors are about uninstalled skills.
    """
    skill_names: list[str] = []
    all_skill_errors = True
    for e in errors:
        m = _SKILL_NOT_INSTALLED_RE.search(e)
        if m:
            skill_names.append(m.group(1))
        else:
            all_skill_errors = False
    if not skill_names or not all_skill_errors:
        return None

    # Extract goal from the last assistant response
    goal = "install missing skills"
    for m in reversed(messages):
        if m["role"] == "assistant":
            try:
                parsed = json.loads(m["content"])
                goal = parsed.get("goal", goal)
            except (json.JSONDecodeError, TypeError):
                pass
            break

    # Build corrected plan: install each missing skill, then replan
    tasks: list[dict] = []
    unique_skills = list(dict.fromkeys(skill_names))  # dedupe preserving order
    for name in unique_skills:
        tasks.append({
            "type": TASK_TYPE_EXEC,
            "detail": f"Install the {name} skill using kiso skill install {name}",
            "skill": None,
            "args": None,
            "expect": "install succeeds",
        })
    tasks.append({
        "type": TASK_TYPE_REPLAN,
        "detail": f"Use {', '.join(unique_skills)} after install",
        "skill": None,
        "args": None,
        "expect": None,
    })

    return {"goal": goal, "secrets": None, "tasks": tasks}


async def run_planner(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_skills: str | list[str] | None = None,
    paraphrased_context: str | None = None,
) -> dict:
    """Run the planner: build context, call LLM, validate, retry if needed.

    Returns the validated plan dict with keys: goal, secrets, tasks.
    Raises PlanError if all retries exhausted.
    """
    messages, installed_names, installed_info = await build_planner_messages(
        db, config, session, user_role, new_message, user_skills=user_skills,
        paraphrased_context=paraphrased_context,
    )
    skills_by_name = {s["name"]: s for s in installed_info}

    max_tasks = int(config.settings["max_plan_tasks"])
    try:
        plan = await _retry_llm_with_validation(
            config, "planner", messages, PLAN_SCHEMA,
            lambda p: validate_plan(p, installed_skills=installed_names, max_tasks=max_tasks,
                                    installed_skills_info=skills_by_name),
            PlanError, "Plan",
            session=session,
        )
    except PlanError as exc:
        # M195: auto-correct when last errors are about uninstalled skills
        last_errors = getattr(exc, "last_errors", [])
        plan = _auto_correct_uninstalled_skills(last_errors, messages)
        if plan is None:
            raise
        log.warning("Auto-corrected plan: replaced uninstalled skill tasks with install + replan")
    log.info("Plan: goal=%r, %d tasks", plan["goal"], len(plan["tasks"]))
    return plan


# ---------------------------------------------------------------------------
# Classifier (fast path)
# ---------------------------------------------------------------------------


class ClassifierError(Exception):
    """Classifier generation failure."""


def build_classifier_messages(content: str) -> list[dict]:
    """Build the message list for the classifier LLM call."""
    return _build_messages(_load_system_prompt("classifier"), content)


async def classify_message(
    config: Config, content: str, session: str = "",
) -> str:
    """Classify a user message as 'plan' or 'chat'.

    Returns ``"plan"`` or ``"chat"``.  On any error or ambiguous output,
    returns ``"plan"`` (safe fallback — the planner handles everything).
    """
    messages = build_classifier_messages(content)
    try:
        raw = await call_llm(config, "classifier", messages, session=session)
    except LLMError as e:
        log.warning("Classifier LLM failed, falling back to plan: %s", e)
        return "plan"

    result = raw.strip().lower()
    if result in ("plan", "chat"):
        log.info("Classifier: %s", result)
        return result

    # Ambiguous output — safe fallback
    log.warning("Classifier returned unexpected value %r, falling back to plan", raw.strip())
    return "plan"


def validate_review(review: dict) -> list[str]:
    """Validate review semantics. Returns list of error strings."""
    errors: list[str] = []
    status = review.get("status")
    if status not in REVIEW_STATUSES:
        errors.append(f"status must be 'ok' or 'replan', got {status!r}")
        return errors
    if status == REVIEW_STATUS_REPLAN and not review.get("reason"):
        errors.append("replan status requires a non-null, non-empty reason")
    return errors


_EXIT_CODE_NOTES: dict[int, str] = {
    1: "Note: exit 1 from grep/which/find/dpkg means 'no matches found', not an error.",
    2: "Note: exit 2 often indicates a usage/syntax error in the command.",
    126: "Note: exit 126 means the command was found but is not executable (permission issue).",
    127: "Note: exit 127 means the command was not found in PATH.",
    -1: "Note: the process was killed (OS error).",
}


def build_reviewer_messages(
    goal: str,
    detail: str,
    expect: str,
    output: str,
    user_message: str,
    success: bool | None = None,
    exit_code: int | None = None,
) -> list[dict]:
    """Build the message list for the reviewer LLM call."""
    system_prompt = _load_system_prompt("reviewer")

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
) -> dict:
    """Run the reviewer on a task output.

    Returns dict with keys: status ("ok" | "replan"), reason, learn.
    Raises ReviewError if all retries exhausted.
    """
    messages = build_reviewer_messages(
        goal, detail, expect, output, user_message,
        success=success, exit_code=exit_code,
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

CURATOR_SCHEMA: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "curator",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "evaluations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "learning_id": {"type": "integer"},
                            "verdict": {
                                "type": "string",
                                "enum": ["promote", "ask", "discard"],
                            },
                            "fact": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "category": {"anyOf": [{"type": "string", "enum": ["project", "user", "tool", "general"]}, {"type": "null"}]},
                            "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "reason": {"type": "string"},
                        },
                        "required": ["learning_id", "verdict", "fact", "category", "question", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["evaluations"],
            "additionalProperties": False,
        },
    },
}


class CuratorError(Exception):
    """Curator validation or generation failure."""


class SummarizerError(Exception):
    """Summarizer generation failure."""


def validate_curator(result: dict, expected_count: int | None = None) -> list[str]:
    """Validate curator result semantics. Returns list of error strings."""
    errors: list[str] = []
    evals = result.get("evaluations", [])
    if expected_count is not None and len(evals) != expected_count:
        errors.append(f"Expected {expected_count} evaluations, got {len(evals)}")
    for i, ev in enumerate(evals, 1):
        verdict = ev.get("verdict")
        if not ev.get("reason"):
            errors.append(f"Evaluation {i}: reason is required")
        if verdict == CURATOR_VERDICT_PROMOTE and not ev.get("fact"):
            errors.append(f"Evaluation {i}: promote verdict requires a non-empty fact")
        if verdict == CURATOR_VERDICT_PROMOTE and ev.get("category") is not None:
            valid_categories = {"project", "user", "tool", "general"}
            if ev["category"] not in valid_categories:
                errors.append(f"Evaluation {i}: category must be one of {sorted(valid_categories)}")
        if verdict == CURATOR_VERDICT_ASK and not ev.get("question"):
            errors.append(f"Evaluation {i}: ask verdict requires a non-empty question")
    return errors


def build_curator_messages(learnings: list[dict]) -> list[dict]:
    """Build the message list for the curator LLM call."""
    system_prompt = _load_system_prompt("curator")
    items = "\n".join(
        f"{i}. [id={l['id']}] {l['content']}"
        for i, l in enumerate(learnings, 1)
    )
    return _build_messages(system_prompt, f"## Learnings\n{items}")


async def run_curator(config: Config, learnings: list[dict], session: str = "") -> dict:
    """Run the curator on pending learnings.

    Returns dict with key "evaluations".
    Raises CuratorError if all retries exhausted.
    """
    messages = build_curator_messages(learnings)
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
    msgs_text = "\n".join(
        f"[{m['role']}] {m.get('user') or 'system'}: {m['content']}"
        for m in messages
    )
    parts: list[str] = []
    if current_summary:
        parts.append(f"## Current Summary\n{current_summary}")
    parts.append(f"## Messages\n{msgs_text}")
    return _build_messages(system_prompt, "\n\n".join(parts))


async def run_summarizer(
    config: Config, current_summary: str, messages: list[dict], session: str = "",
) -> str:
    """Run the summarizer. Returns the new summary string.

    Raises SummarizerError on failure.
    """
    msgs = build_summarizer_messages(current_summary, messages)
    try:
        return await call_llm(config, "summarizer", msgs, session=session)
    except LLMError as e:
        raise SummarizerError(f"LLM call failed: {e}")


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
    """Run the paraphraser on untrusted messages. Returns paraphrased text.

    Raises ParaphraserError on failure.
    """
    msgs = build_paraphraser_messages(messages)
    try:
        return await call_llm(config, "paraphraser", msgs, session=session)
    except LLMError as e:
        raise ParaphraserError(f"LLM call failed: {e}")


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
    """
    bot_name = config.settings["bot_name"]
    system_prompt = _load_system_prompt("messenger").replace("{bot_name}", bot_name)

    context_parts: list[str] = []
    if goal:
        context_parts.append(f"## Current User Request\n{goal}")
    if summary:
        context_parts.append(f"## Session Summary (background only)\n{summary}")
    if facts:
        facts_text = "\n".join(f"- {f['content']}" for f in facts)
        context_parts.append(f"## Known Facts\n{facts_text}")
    if recent_messages:
        msgs_text = "\n".join(
            f"[{m['role']}] {m.get('user') or 'system'}: {m['content']}"
            for m in recent_messages
        )
        context_parts.append(
            f"## Recent Conversation\n{fence_content(msgs_text, 'MESSAGES')}"
        )
    if plan_outputs_text:
        context_parts.append(f"## Preceding Task Outputs\n{plan_outputs_text}")
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
) -> str:
    """Run the messenger: generate a user-facing response.

    Loads session summary and facts, builds context, and calls the
    messenger LLM to produce text for the user.

    When *goal* is provided it is included as ``## Current User Request``
    so the messenger knows the original intent behind the plan.

    When *include_recent* is True (chat fast-path), recent conversation
    messages are injected so the messenger can reference prior exchanges
    instead of hallucinating.

    Returns the generated text.
    Raises MessengerError on failure.
    """
    sess = await get_session(db, session)
    summary = sess["summary"] if sess else ""
    facts = await get_facts(db, session=session, limit=_MAX_MESSENGER_FACTS)
    recent = None
    if include_recent:
        context_limit = int(config.settings["context_messages"])
        recent = await get_recent_messages(db, session, limit=context_limit)
    messages = build_messenger_messages(
        config, summary, facts, detail, plan_outputs_text, goal=goal,
        recent_messages=recent or None,
    )
    try:
        return await call_llm(config, "messenger", messages, session=session)
    except LLMError as e:
        raise MessengerError(f"LLM call failed: {e}")


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
    if context:
        parts.append(f"## Context\n{context}")
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
    try:
        return await call_llm(config, "searcher", messages, session=session)
    except LLMError as e:
        raise SearcherError(f"LLM call failed: {e}")


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
    context_parts: list[str] = []
    context_parts.append(f"## System Environment\n{sys_env_text}")
    if plan_outputs_text:
        context_parts.append(f"## Preceding Task Outputs\n{plan_outputs_text}")
    if retry_context:
        context_parts.append(f"## Retry Context\n{retry_context}")
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
    try:
        raw = await call_llm(config, "worker", messages, session=session, max_tokens=500)
    except LLMError as e:
        raise ExecTranslatorError(f"LLM call failed: {e}")

    command = raw.strip()
    if not command or command == "CANNOT_TRANSLATE":
        raise ExecTranslatorError(
            f"Cannot translate task to shell command: {detail}"
        )
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
    facts_text = "\n".join(f"- {f['content']}" for f in facts)
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
