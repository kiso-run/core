"""Planner brain — builds context, calls LLM, validates plan."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from kiso.config import Config, KISO_DIR
from kiso.llm import LLMError, call_llm
from kiso.security import fence_content
from kiso.skills import discover_skills, build_planner_skill_list
from kiso.store import get_facts, get_pending_items, get_recent_messages, get_session
from kiso.sysenv import get_system_env, build_system_env_section

log = logging.getLogger(__name__)


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
                                "enum": ["exec", "msg", "skill"],
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
            },
            "required": ["goal", "secrets", "tasks"],
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
                "learn": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["status", "reason", "learn"],
            "additionalProperties": False,
        },
    },
}


class ReviewError(Exception):
    """Review validation or generation failure."""


class PlanError(Exception):
    """Plan validation or generation failure."""


_ROLES_DIR = Path(__file__).parent / "roles"


def _load_system_prompt(role: str) -> str:
    """Load system prompt: user override first, then package default."""
    # User override
    user_path = KISO_DIR / "roles" / f"{role}.md"
    if user_path.exists():
        return user_path.read_text()
    # Package default
    pkg_path = _ROLES_DIR / f"{role}.md"
    if pkg_path.exists():
        return pkg_path.read_text()
    raise FileNotFoundError(f"No prompt found for role '{role}'")


def validate_plan(
    plan: dict,
    installed_skills: list[str] | None = None,
    max_tasks: int | None = None,
) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid).

    If installed_skills is provided, skill tasks are validated against it.
    If max_tasks is provided, plans with more tasks are rejected.
    """
    errors: list[str] = []
    tasks = plan.get("tasks", [])

    if not tasks:
        errors.append("tasks list must not be empty")
        return errors

    if max_tasks and len(tasks) > max_tasks:
        errors.append(f"Plan has {len(tasks)} tasks, max allowed is {max_tasks}")

    for i, task in enumerate(tasks, 1):
        t = task.get("type")
        if t not in ("exec", "msg", "skill"):
            errors.append(f"Task {i}: unknown type {t!r}")
            continue
        if t in ("exec", "skill") and task.get("expect") is None:
            errors.append(f"Task {i}: {t} task must have a non-null expect")
        if t == "msg" and task.get("expect") is not None:
            errors.append(f"Task {i}: msg task must have expect = null")
        if t == "skill":
            skill_name = task.get("skill")
            if not skill_name:
                errors.append(f"Task {i}: skill task must have a non-null skill name")
            elif installed_skills is not None and skill_name not in installed_skills:
                errors.append(f"Task {i}: skill '{skill_name}' is not installed")

    last = tasks[-1]
    if last.get("type") != "msg":
        errors.append("Last task must be type 'msg'")

    return errors


async def build_planner_messages(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_skills: str | list[str] | None = None,
    paraphrased_context: str | None = None,
) -> list[dict]:
    """Build the message list for the planner LLM call."""
    system_prompt = _load_system_prompt("planner")

    # Context pieces
    sess = await get_session(db, session)
    summary = sess["summary"] if sess else ""
    facts = await get_facts(db)
    pending = await get_pending_items(db, session)
    context_limit = int(config.settings.get("context_messages", 5))
    recent = await get_recent_messages(db, session, limit=context_limit)

    # Build context block
    context_parts: list[str] = []

    if summary:
        context_parts.append(f"## Session Summary\n{summary}")

    if facts:
        facts_text = "\n".join(f"- {f['content']}" for f in facts)
        context_parts.append(f"## Known Facts\n{facts_text}")

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
    skill_list = build_planner_skill_list(installed, user_role, user_skills)
    if skill_list:
        context_parts.append(f"## Skills\n{skill_list}")

    context_parts.append(f"## Caller Role\n{user_role}")
    context_parts.append(f"## New Message\n{fence_content(new_message, 'USER_MSG')}")

    context_block = "\n\n".join(context_parts)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_block},
    ]


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
    max_retries = int(config.settings.get("max_validation_retries", 3))
    messages = await build_planner_messages(
        db, config, session, user_role, new_message, user_skills=user_skills,
        paraphrased_context=paraphrased_context,
    )

    # Get installed skill names for plan validation
    installed = discover_skills()
    installed_names = [s["name"] for s in installed]

    last_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        if last_errors:
            error_feedback = "Your plan has errors:\n" + "\n".join(
                f"- {e}" for e in last_errors
            ) + "\nFix these and return the corrected plan."
            messages.append({"role": "user", "content": error_feedback})

        try:
            raw = await call_llm(config, "planner", messages, response_format=PLAN_SCHEMA, session=session)
        except LLMError as e:
            raise PlanError(f"LLM call failed: {e}")

        try:
            plan = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as e:
            raise PlanError(f"Planner returned invalid JSON: {e}")

        max_tasks = int(config.settings.get("max_plan_tasks", 20))
        errors = validate_plan(plan, installed_skills=installed_names, max_tasks=max_tasks)
        if not errors:
            log.info("Plan accepted (attempt %d): goal=%r, %d tasks",
                     attempt, plan["goal"], len(plan["tasks"]))
            return plan

        log.warning("Plan validation failed (attempt %d/%d): %s",
                    attempt, max_retries, errors)
        last_errors = errors
        # Add assistant response to conversation for retry
        messages.append({"role": "assistant", "content": raw})

    raise PlanError(
        f"Plan validation failed after {max_retries} attempts: {last_errors}"
    )


def validate_review(review: dict) -> list[str]:
    """Validate review semantics. Returns list of error strings."""
    errors: list[str] = []
    status = review.get("status")
    if status not in ("ok", "replan"):
        errors.append(f"status must be 'ok' or 'replan', got {status!r}")
        return errors
    if status == "replan" and not review.get("reason"):
        errors.append("replan status requires a non-null, non-empty reason")
    return errors


async def build_reviewer_messages(
    goal: str,
    detail: str,
    expect: str,
    output: str,
    user_message: str,
    success: bool | None = None,
) -> list[dict]:
    """Build the message list for the reviewer LLM call."""
    system_prompt = _load_system_prompt("reviewer")

    context = (
        f"## Plan Goal\n{goal}\n\n"
        f"## Task Detail\n{detail}\n\n"
        f"## Expected Outcome\n{expect}\n\n"
        f"## Actual Output\n{fence_content(output, 'TASK_OUTPUT')}\n\n"
        f"## Original User Message\n{fence_content(user_message, 'USER_MSG')}"
    )

    if success is not None:
        status_text = "succeeded (exit code 0)" if success else "FAILED (non-zero exit code)"
        context += f"\n\n## Command Status\n{status_text}"

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]


async def run_reviewer(
    config: Config,
    goal: str,
    detail: str,
    expect: str,
    output: str,
    user_message: str,
    session: str = "",
    success: bool | None = None,
) -> dict:
    """Run the reviewer on a task output.

    Returns dict with keys: status ("ok" | "replan"), reason, learn.
    Raises ReviewError if all retries exhausted.
    """
    max_retries = int(config.settings.get("max_validation_retries", 3))
    messages = await build_reviewer_messages(goal, detail, expect, output, user_message, success=success)
    last_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        if last_errors:
            error_feedback = "Your review has errors:\n" + "\n".join(
                f"- {e}" for e in last_errors
            ) + "\nFix these and return the corrected review."
            messages.append({"role": "user", "content": error_feedback})

        try:
            raw = await call_llm(config, "reviewer", messages, response_format=REVIEW_SCHEMA, session=session)
        except LLMError as e:
            raise ReviewError(f"LLM call failed: {e}")

        try:
            review = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as e:
            raise ReviewError(f"Reviewer returned invalid JSON: {e}")

        errors = validate_review(review)
        if not errors:
            log.info("Review accepted (attempt %d): status=%s", attempt, review["status"])
            return review

        log.warning("Review validation failed (attempt %d/%d): %s",
                    attempt, max_retries, errors)
        last_errors = errors
        messages.append({"role": "assistant", "content": raw})

    raise ReviewError(
        f"Review validation failed after {max_retries} attempts: {last_errors}"
    )


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
                            "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "reason": {"type": "string"},
                        },
                        "required": ["learning_id", "verdict", "fact", "question", "reason"],
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
        if verdict == "promote" and not ev.get("fact"):
            errors.append(f"Evaluation {i}: promote verdict requires a non-empty fact")
        if verdict == "ask" and not ev.get("question"):
            errors.append(f"Evaluation {i}: ask verdict requires a non-empty question")
    return errors


def build_curator_messages(learnings: list[dict]) -> list[dict]:
    """Build the message list for the curator LLM call."""
    system_prompt = _load_system_prompt("curator")
    items = "\n".join(
        f"{i}. [id={l['id']}] {l['content']}"
        for i, l in enumerate(learnings, 1)
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"## Learnings\n{items}"},
    ]


async def run_curator(config: Config, learnings: list[dict], session: str = "") -> dict:
    """Run the curator on pending learnings.

    Returns dict with key "evaluations".
    Raises CuratorError if all retries exhausted.
    """
    max_retries = int(config.settings.get("max_validation_retries", 3))
    messages = build_curator_messages(learnings)
    last_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        if last_errors:
            error_feedback = "Your evaluations have errors:\n" + "\n".join(
                f"- {e}" for e in last_errors
            ) + "\nFix these and return the corrected evaluations."
            messages.append({"role": "user", "content": error_feedback})

        try:
            raw = await call_llm(config, "curator", messages, response_format=CURATOR_SCHEMA, session=session)
        except LLMError as e:
            raise CuratorError(f"LLM call failed: {e}")

        try:
            result = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as e:
            raise CuratorError(f"Curator returned invalid JSON: {e}")

        errors = validate_curator(result, expected_count=len(learnings))
        if not errors:
            log.info("Curator accepted (attempt %d): %d evaluations",
                     attempt, len(result["evaluations"]))
            return result

        log.warning("Curator validation failed (attempt %d/%d): %s",
                    attempt, max_retries, errors)
        last_errors = errors
        messages.append({"role": "assistant", "content": raw})

    raise CuratorError(
        f"Curator validation failed after {max_retries} attempts: {last_errors}"
    )


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------

def build_summarizer_messages(
    current_summary: str, messages: list[dict]
) -> list[dict]:
    """Build the message list for the summarizer LLM call."""
    system_prompt = _load_system_prompt("summarizer")
    msgs_text = "\n".join(
        f"[{m['role']}] {m.get('user') or 'system'}: {m['content']}"
        for m in messages
    )
    parts: list[str] = []
    if current_summary:
        parts.append(f"## Current Summary\n{current_summary}")
    parts.append(f"## Messages\n{msgs_text}")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


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
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(lines)},
    ]


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
) -> list[dict]:
    """Build the message list for the messenger LLM call.

    Args:
        config: Application config (reads bot_name from settings).
        summary: Current session summary.
        facts: Known facts from the knowledge base.
        detail: The msg task detail (what to communicate).
        plan_outputs_text: Pre-formatted preceding task outputs (from worker).
        goal: The plan goal (user's original request for this turn).
    """
    bot_name = config.settings.get("bot_name", "Kiso")
    system_prompt = _load_system_prompt("messenger").replace("{bot_name}", bot_name)

    context_parts: list[str] = []
    if goal:
        context_parts.append(f"## Current User Request\n{goal}")
    if summary:
        context_parts.append(f"## Session Summary (background only)\n{summary}")
    if facts:
        facts_text = "\n".join(f"- {f['content']}" for f in facts)
        context_parts.append(f"## Known Facts\n{facts_text}")
    if plan_outputs_text:
        context_parts.append(f"## Preceding Task Outputs\n{plan_outputs_text}")
    context_parts.append(f"## Task\n{detail}")

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(context_parts)},
    ]


async def run_messenger(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    detail: str,
    plan_outputs_text: str = "",
    goal: str = "",
) -> str:
    """Run the messenger: generate a user-facing response.

    Loads session summary and facts, builds context, and calls the
    messenger LLM to produce text for the user.

    When *goal* is provided it is included as ``## Current User Request``
    so the messenger knows the original intent behind the plan.

    Returns the generated text.
    Raises MessengerError on failure.
    """
    sess = await get_session(db, session)
    summary = sess["summary"] if sess else ""
    facts = await get_facts(db)
    messages = build_messenger_messages(
        config, summary, facts, detail, plan_outputs_text, goal=goal,
    )
    try:
        return await call_llm(config, "messenger", messages, session=session)
    except LLMError as e:
        raise MessengerError(f"LLM call failed: {e}")


# ---------------------------------------------------------------------------
# Exec translator  (planner = architect, worker/translator = editor)
# ---------------------------------------------------------------------------

def build_exec_translator_messages(
    config: Config,
    detail: str,
    sys_env_text: str,
    plan_outputs_text: str = "",
) -> list[dict]:
    """Build the message list for the exec translator LLM call."""
    system_prompt = _load_system_prompt("exec_translator")
    context_parts: list[str] = []
    context_parts.append(f"## System Environment\n{sys_env_text}")
    if plan_outputs_text:
        context_parts.append(f"## Preceding Task Outputs\n{plan_outputs_text}")
    context_parts.append(f"## Task\n{detail}")

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(context_parts)},
    ]


async def run_exec_translator(
    config: Config,
    detail: str,
    sys_env_text: str,
    plan_outputs_text: str = "",
    session: str = "",
) -> str:
    """Translate a natural-language exec task detail into a shell command.

    Returns the shell command string.
    Raises ExecTranslatorError on failure.
    """
    messages = build_exec_translator_messages(
        config, detail, sys_env_text, plan_outputs_text,
    )
    try:
        raw = await call_llm(config, "worker", messages, session=session)
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
) -> list[str]:
    """Consolidate/deduplicate facts via LLM. Returns list of consolidated fact strings.

    Raises SummarizerError on failure.
    """
    system_prompt = _load_system_prompt("fact_consolidation")
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
    return result
