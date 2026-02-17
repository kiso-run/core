"""Planner brain — builds context, calls LLM, validates plan."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from kiso.config import Config, KISO_DIR
from kiso.llm import LLMError, call_llm
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


class PlanError(Exception):
    """Plan validation or generation failure."""


def _load_system_prompt(role: str) -> str:
    """Load system prompt from ~/.kiso/roles/{role}.md, or return default."""
    path = KISO_DIR / "roles" / f"{role}.md"
    if path.exists():
        return path.read_text()
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


def validate_plan(plan: dict) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid)."""
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
        # Skill validation (M7): skip for now, no skills installed

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

    # Skills: empty for now (M7)
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
) -> dict:
    """Run the planner: build context, call LLM, validate, retry if needed.

    Returns the validated plan dict with keys: goal, secrets, tasks.
    Raises PlanError if all retries exhausted.
    """
    max_retries = int(config.settings.get("max_validation_retries", 3))
    messages = await build_planner_messages(db, config, session, user_role, new_message)
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

        errors = validate_plan(plan)
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
