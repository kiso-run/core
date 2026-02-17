"""Planner brain — builds context, calls LLM, validates plan."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from kiso.config import Config, KISO_DIR
from kiso.llm import LLMError, call_llm
from kiso.skills import discover_skills, build_planner_skill_list
from kiso.store import get_facts, get_pending_items, get_recent_messages, get_session

log = logging.getLogger(__name__)

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
                    "type": ["array", "null"],
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
                            "skill": {"type": ["string", "null"]},
                            "args": {"type": ["string", "null"]},
                            "expect": {"type": ["string", "null"]},
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
                "reason": {"type": ["string", "null"]},
                "learn": {"type": ["string", "null"]},
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


_DEFAULT_PROMPTS: dict[str, callable] = {}


def _load_system_prompt(role: str) -> str:
    """Load system prompt from ~/.kiso/roles/{role}.md, or return default."""
    path = KISO_DIR / "roles" / f"{role}.md"
    if path.exists():
        return path.read_text()
    defaults = {
        "planner": _default_planner_prompt,
        "reviewer": _default_reviewer_prompt,
    }
    factory = defaults.get(role)
    if factory:
        return factory()
    return _default_planner_prompt()


def _default_planner_prompt() -> str:
    return """\
You are a task planner. Given a user message, produce a JSON plan with:
- goal: high-level objective
- secrets: null (or array of {key, value} if user shares credentials)
- tasks: array of tasks to accomplish the goal

Task types:
- exec: shell command. detail = the command. expect = success criteria (required).
- skill: call a skill. detail = what to do. skill = name. args = JSON string. expect = success criteria (required).
- msg: message to user. detail = what to communicate. skill/args/expect = null.

Rules:
- The last task MUST be type "msg" (user always gets a response)
- exec and skill tasks MUST have a non-null expect field
- msg tasks MUST have expect = null
- task detail must be self-contained (the worker won't see the conversation)
- If the request is unclear, produce a single msg task asking for clarification
- tasks list must not be empty
"""


def validate_plan(plan: dict, installed_skills: list[str] | None = None) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid).

    If installed_skills is provided, skill tasks are validated against it.
    """
    errors: list[str] = []
    tasks = plan.get("tasks", [])

    if not tasks:
        errors.append("tasks list must not be empty")
        return errors

    for i, task in enumerate(tasks, 1):
        t = task.get("type")
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

    if pending:
        pending_text = "\n".join(f"- {p['content']}" for p in pending)
        context_parts.append(f"## Pending Questions\n{pending_text}")

    if recent:
        msgs_text = "\n".join(
            f"[{m['role']}] {m['user'] or 'system'}: {m['content']}"
            for m in recent
        )
        context_parts.append(f"## Recent Messages\n{msgs_text}")

    # Skill discovery — rescan on each planner call
    installed = discover_skills()
    skill_list = build_planner_skill_list(installed, user_role, user_skills)
    if skill_list:
        context_parts.append(f"## Skills\n{skill_list}")

    context_parts.append(f"## Caller Role\n{user_role}")
    context_parts.append(f"## New Message\n{new_message}")

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
) -> dict:
    """Run the planner: build context, call LLM, validate, retry if needed.

    Returns the validated plan dict with keys: goal, secrets, tasks.
    Raises PlanError if all retries exhausted.
    """
    max_retries = int(config.settings.get("max_validation_retries", 3))
    messages = await build_planner_messages(
        db, config, session, user_role, new_message, user_skills=user_skills,
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
            raw = await call_llm(config, "planner", messages, response_format=PLAN_SCHEMA)
        except LLMError as e:
            raise PlanError(f"LLM call failed: {e}")

        try:
            plan = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlanError(f"Planner returned invalid JSON: {e}")

        errors = validate_plan(plan, installed_skills=installed_names)
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


def _default_reviewer_prompt() -> str:
    return """\
You are a task reviewer. Given a task and its output, determine if the task succeeded.

You receive:
- The plan goal
- The task detail (what was requested)
- The task expect (success criteria)
- The task output (what actually happened)
- The original user message

Return a JSON object:
- status: "ok" if the task succeeded, "replan" if it failed and needs a new plan
- reason: if replan, explain why (required). If ok, null.
- learn: if you learned something useful about the system/project/user, state it concisely. Otherwise null.

Rules:
- Be strict: if the output doesn't match the expect criteria, mark as replan.
- Be concise in your reason — the planner will use it to create a better plan.
- Only learn genuinely useful facts (e.g. "project uses Python 3.12", "database is PostgreSQL").
  Do not learn transient facts (e.g. "command failed", "file not found").
"""


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
) -> list[dict]:
    """Build the message list for the reviewer LLM call."""
    system_prompt = _load_system_prompt("reviewer")

    context = (
        f"## Plan Goal\n{goal}\n\n"
        f"## Task Detail\n{detail}\n\n"
        f"## Expected Outcome\n{expect}\n\n"
        f"## Actual Output\n```\n{output}\n```\n\n"
        f"## Original User Message\n{user_message}"
    )

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
) -> dict:
    """Run the reviewer on a task output.

    Returns dict with keys: status ("ok" | "replan"), reason, learn.
    Raises ReviewError if all retries exhausted.
    """
    max_retries = int(config.settings.get("max_validation_retries", 3))
    messages = await build_reviewer_messages(goal, detail, expect, output, user_message)
    last_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        if last_errors:
            error_feedback = "Your review has errors:\n" + "\n".join(
                f"- {e}" for e in last_errors
            ) + "\nFix these and return the corrected review."
            messages.append({"role": "user", "content": error_feedback})

        try:
            raw = await call_llm(config, "reviewer", messages, response_format=REVIEW_SCHEMA)
        except LLMError as e:
            raise ReviewError(f"LLM call failed: {e}")

        try:
            review = json.loads(raw)
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
