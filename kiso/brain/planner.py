"""Planner-specific validation and planning prompt assembly."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiosqlite

from kiso.config import Config, setting_bool, setting_int
from kiso.connectors import discover_connectors
from kiso.skill_loader import discover_skills
from kiso.skill_runtime import (
    filter_by_activation_hints,
    instructions_for_planner,
    metadata_for_briefer,
)
from kiso.security import fence_content
from kiso.store import (
    _normalize_entity_name,
    get_all_entities,
    get_all_tags,
    get_behavior_facts,
    get_pending_items,
    get_recent_messages,
    get_safety_facts,
    get_session,
    get_session_project_id,
    search_facts,
    search_facts_by_entity,
    search_facts_scored,
)
from kiso.sysenv import (
    build_install_context,
    build_system_env_essential,
    build_system_env_section,
    build_user_settings_text,
    get_system_env,
)
from .common import (
    BRIEFER_MODULES,
    PLAN_SCHEMA,
    MemoryPack,
    PlanError,
    PlannerPromptState,
    TASK_TYPE_EXEC,
    TASK_TYPE_MCP,
    TASK_TYPE_MSG,
    TASK_TYPE_REPLAN,
    TASK_TYPES,
    _INSTALL_CMD_RE,
    _INSTALL_MODE_NONE,
    _MIN_PROMOTED_FACT_LEN,
    _NPM_GLOBAL_RE,
    _NPX_RE,
    _PIP_INSTALL_RE,
    _SYSTEM_INSTALL_HINT_RE,
    _TYPE_EXAMPLES,
    _UV_PIP_RE,
    _VALID_FACT_CATEGORIES,
    _add_context_section,
    _add_section,
    _build_install_mode_context,
    _build_messages,
    _build_planner_memory_pack,
    _classify_install_mode,
    _format_pending_items,
    _join_or_empty,
    _load_modular_prompt,
    _merge_context_sections,
    _normalize_install_target_token,
    _prefilter_context_pool,
    _retry_llm_with_validation,
    _GIT_URL_RE,
    build_recent_context,
    format_mcp_catalog,
    format_mcp_prompts,
    format_mcp_resources,
    run_briefer,
)

if TYPE_CHECKING:
    from typing import Any

    from kiso.worker.utils import ExecutionState

log = logging.getLogger("kiso.brain")

_IMPORTED_NAMES = set(globals())

_KISO_CMD_KEYWORDS = frozenset({"mcp", "skill", "env", "instance", "kiso"})
_USER_MGMT_KEYWORDS = frozenset({"user", "admin", "alias"})

def _validate_plan_structure(
    plan: dict, max_tasks: int | None, is_replan: bool,
) -> tuple[list[str], list[dict]]:
    """Check top-level plan fields and strip extend_replan from initial plans.

    Returns (errors, tasks) so callers can short-circuit on structural failures.

    Side effect (M1591): `extend_replan` (top-level) and `task["group"]`
    (per-task) string values are coerced to int in place. V4-Flash
    json_object mode sometimes emits stringified ints despite the
    strict schema; downstream consumers compare/index on real ints.
    Non-numeric strings are left as-is and will surface via the schema
    or per-field validators.
    """
    if not is_replan:
        plan.pop("extend_replan", None)
    elif isinstance(plan.get("extend_replan"), str):
        try:
            plan["extend_replan"] = int(plan["extend_replan"])
        except ValueError:
            pass
    errors: list[str] = []
    tasks = plan.get("tasks", [])
    if not tasks:
        errors.append("tasks list must not be empty")
    elif max_tasks is not None and len(tasks) > max_tasks:
        errors.append(f"Plan has {len(tasks)} tasks, max allowed is {max_tasks}")
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("group"), str):
            try:
                task["group"] = int(task["group"])
            except ValueError:
                pass
    return errors, tasks


# Exec details starting with these phrases are analytical (not shell-translatable).
# Exception: if the detail also contains a `/` path or known binary, allow it
# (e.g., "Verify that /tmp/output.txt exists" → `test -f /tmp/output.txt`).
_NON_ACTIONABLE_PREFIXES = (
    "check the content", "identify ", "determine ", "analyze ",
    "validate the ", "verify the content", "inspect the content",
    "review the ", "understand ", "evaluate ",
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


def _is_non_actionable_exec(detail: str) -> bool:
    """Return True if exec detail is analytical rather than shell-actionable."""
    lower = detail.lower().strip()
    if not any(lower.startswith(p) for p in _NON_ACTIONABLE_PREFIXES):
        return False
    # Allow if detail contains a concrete path or known binary
    if "/" in detail:
        return False
    return True


def _mentions_user_delivery(detail: str) -> bool:
    """Return True when an action-task detail includes user-delivery wording."""
    return bool(_ACTION_TO_USER_RE.search(detail or ""))


def _validate_plan_tasks(
    tasks: list[dict],
    installed_skills: list[str] | None,
    installed_skills_info: dict[str, dict] | None,
    install_approved: bool = False,
    mcp_methods_pool: dict[str, list] | None = None,
) -> list[str]:
    """Check per-task rules: type, detail, expect, args validation."""
    errors: list[str] = []
    replan_count = 0
    # M1608 structural backstop: plan-level fields must not appear on
    # tasks. The worker reads these flags off the plan; a task-level
    # value is silently ignored, so the LLM thinks it has paused or
    # proposed install but the actual plan is still an action plan.
    # The only safe response is to reject the misplacement with a clear
    # error so the LLM corrects it on the next attempt.
    _PLAN_ONLY_FIELDS = ("awaits_input", "kb_answer", "needs_install", "knowledge")
    for i, task in enumerate(tasks, 1):
        for field in _PLAN_ONLY_FIELDS:
            if field in task:
                errors.append(
                    f"Task {i}: '{field}' is a plan-level field, not a task "
                    f"field — move it to the plan level (the worker reads "
                    f"the plan flag, not task-level values)."
                )
        t = task.get("type")
        if t not in TASK_TYPES:
            errors.append(f"Task {i}: unknown type {t!r}")
            continue
        if t in (TASK_TYPE_EXEC, TASK_TYPE_MCP) and task.get("expect") is None:
            errors.append(
                f"Task {i}: {t} task must have expect describing WHAT RESULT you need "
                f"(e.g., 'list of search results', 'file created successfully')"
            )
        # Cross-type hygiene: server/method fields may only appear on mcp tasks
        if t != TASK_TYPE_MCP:
            if task.get("server") is not None or task.get("method") is not None:
                errors.append(
                    f"Task {i}: {t} task must have server=null and method=null "
                    f"(server/method are reserved for mcp tasks)"
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
                f"rewrite as a concrete shell command description"
            )
        if t == TASK_TYPE_EXEC and _PIP_INSTALL_RE.search(detail) and not _UV_PIP_RE.search(detail):
            errors.append(
                f"Task {i}: use 'uv pip install' instead of bare 'pip install'. "
                f"Direct pip can corrupt the system environment."
            )
        if t == TASK_TYPE_EXEC and _NPM_GLOBAL_RE.search(detail) and not _NPX_RE.search(detail):
            errors.append(
                f"Task {i}: use 'npx -y <pkg>' instead of 'npm install -g <pkg>'. "
                f"Global npm installs pollute the runtime; npx -y runs ephemerally "
                f"and is the right default for one-shot tools and MCP servers."
            )
        if t == TASK_TYPE_EXEC and _mentions_user_delivery(detail):
            errors.append(
                f"Task {i}: action task detail includes user-delivery wording. "
                f"Action tasks should do the work only; use a final msg task "
                f"to tell/send results to the user."
            )
        if t == TASK_TYPE_MSG:
            for field in ("expect", "args"):
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
        if t == TASK_TYPE_REPLAN:
            replan_count += 1
            if task.get("expect") is not None:
                errors.append(f"Task {i}: replan task must have expect = null")
            if task.get("args") is not None:
                errors.append(f"Task {i}: replan task must have args = null")
            if i != len(tasks):
                errors.append(f"Task {i}: replan task can only be the last task")
        if t == TASK_TYPE_MCP:
            server = task.get("server")
            method = task.get("method")
            if not isinstance(server, str) or not server:
                errors.append(
                    f"Task {i}: mcp task requires a non-empty 'server' field "
                    f"(the name of a configured [mcp.<server>] entry)"
                )
            if not isinstance(method, str) or not method:
                errors.append(
                    f"Task {i}: mcp task requires a non-empty 'method' field "
                    f"(the name of a method exposed by the server)"
                )
            if method == "__resource_read":
                args_raw = task.get("args")
                if not isinstance(args_raw, dict):
                    errors.append(
                        f"Task {i}: mcp __resource_read requires an args "
                        f"object with a single 'uri' string"
                    )
                else:
                    uri = args_raw.get("uri")
                    if not isinstance(uri, str) or not uri:
                        errors.append(
                            f"Task {i}: mcp __resource_read requires "
                            f"args.uri to be a non-empty string"
                        )
                    extra = set(args_raw) - {"uri"}
                    if extra:
                        errors.append(
                            f"Task {i}: mcp __resource_read args must "
                            f"contain only 'uri' (got extras: {sorted(extra)})"
                        )
            elif method == "__prompt_get":
                args_raw = task.get("args")
                if not isinstance(args_raw, dict):
                    errors.append(
                        f"Task {i}: mcp __prompt_get requires an args "
                        f"object with a 'name' string and optional "
                        f"'prompt_args' object"
                    )
                else:
                    pname = args_raw.get("name")
                    if not isinstance(pname, str) or not pname:
                        errors.append(
                            f"Task {i}: mcp __prompt_get requires "
                            f"args.name to be a non-empty string"
                        )
                    if "prompt_args" in args_raw and not isinstance(
                        args_raw.get("prompt_args"), dict
                    ):
                        errors.append(
                            f"Task {i}: mcp __prompt_get args.prompt_args "
                            f"must be a JSON object when present"
                        )
                    extra = set(args_raw) - {"name", "prompt_args"}
                    if extra:
                        errors.append(
                            f"Task {i}: mcp __prompt_get args must "
                            f"contain only 'name' and optional 'prompt_args' "
                            f"(got extras: {sorted(extra)})"
                        )
            elif (
                mcp_methods_pool is not None
                and isinstance(server, str)
                and isinstance(method, str)
            ):
                methods = mcp_methods_pool.get(server)
                if methods is None:
                    errors.append(
                        f"Task {i}: mcp server {server!r} is not available "
                        f"(not configured, disabled, or marked unhealthy)"
                    )
                else:
                    known = {m.name for m in methods}
                    if method not in known:
                        errors.append(
                            f"Task {i}: mcp method {server}:{method} does not "
                            f"exist on this server "
                            f"(known methods: {sorted(known)[:5]})"
                        )
                    else:
                        from kiso.mcp.validate import validate_mcp_args

                        target = next(m for m in methods if m.name == method)
                        args_raw = task.get("args")
                        if args_raw is None:
                            args_dict = {}
                        elif isinstance(args_raw, dict):
                            args_dict = args_raw
                        else:
                            errors.append(
                                f"Task {i}: mcp args must be a JSON object"
                            )
                            args_dict = None  # type: ignore[assignment]
                        if args_dict is not None:
                            for msg in validate_mcp_args(target.input_schema, args_dict):
                                errors.append(
                                    f"Task {i}: mcp args invalid against "
                                    f"{server}:{method} inputSchema: {msg}"
                                )

    if replan_count > 1:
        errors.append("A plan can have at most one replan task")

    return errors


# goal-plan mismatch — detect artifact requests with no exec/mcp task.
_ARTIFACT_VERBS = frozenset({"create", "write", "generate", "build", "produce", "make"})
_ARTIFACT_NOUNS = frozenset({
    "file", "document", "script", "markdown", "csv", "report",
    "table", "spreadsheet", "config", "template", "page",
})


def _validate_plan_ordering(
    tasks: list[dict], is_replan: bool, install_approved: bool,
    has_needs_install: bool = False,
    has_knowledge: bool = False,
    has_kb_answer: bool = False,
    has_awaits_input: bool = False,
    allow_msg_only: bool = False,
) -> list[str]:
    """Check cross-task ordering rules and install safety."""
    errors: list[str] = []

    # msg-only plans are rejected unless one of the escape flags is set:
    # needs_install (install proposal), knowledge (storage), kb_answer
    # (KB recall from briefer context), awaits_input (broker pause for
    # user input — M1579a), or allow_msg_only (structural fallback).
    _DATA_TYPES = {TASK_TYPE_EXEC, TASK_TYPE_REPLAN, TASK_TYPE_MCP}
    has_action = any(t.get("type") in _DATA_TYPES for t in tasks)
    if not has_action and not is_replan:
        if (
            not has_needs_install
            and not has_knowledge
            and not has_kb_answer
            and not has_awaits_input
            and not allow_msg_only
        ):
            errors.append(
                "Plan has only msg tasks — include at least one "
                "exec/mcp task for action requests. "
                "Msg-only is valid only for install proposals "
                "(set needs_install), knowledge storage, KB recall "
                "(set kb_answer when answering from briefer context), "
                "or pausing for user input (set awaits_input)."
            )

    # msg as first task wastes an LLM call before any action runs.
    # skip when needs_install — install validators give targeted feedback.
    if has_action and tasks[0].get("type") == TASK_TYPE_MSG and not has_needs_install:
        errors.append(
            "msg task must come after action tasks — do not start "
            "with an announcement msg. The user already sees the plan."
        )

    # install execs allowed in replans, when user approved in prior msg cycle,
    # or when user directly requested install (needs_install is empty — no proposal).
    # only block when needs_install IS set (mixed propose+install in same plan).
    if not is_replan and not install_approved and has_needs_install:
        first_install_idx = next(
            (i for i, t in enumerate(tasks)
             if t.get("type") == TASK_TYPE_EXEC and _INSTALL_CMD_RE.search(t.get("detail", ""))),
            None,
        )
        if first_install_idx is not None:
            errors.append(
                f"Task {first_install_idx + 1}: installs a package in the first plan. "
                f"You CANNOT install in the same plan that asks for permission — the user "
                f"hasn't replied yet. Plan a SINGLE msg task asking whether to install, "
                f"offer alternatives, and end the plan there. The install happens in the "
                f"next cycle after the user approves."
            )

    # after installing a package that was proposed in a prior turn, the
    # original request is still pending — must replan to continue with it.
    if install_approved:
        has_install_exec = any(
            t.get("type") == TASK_TYPE_EXEC
            and _INSTALL_CMD_RE.search(t.get("detail", ""))
            for t in tasks
        )
        if has_install_exec and tasks[-1].get("type") == TASK_TYPE_MSG:
            errors.append(
                "Plan installs a package after user approval but ends with msg. "
                "The original request is still pending — use replan as the last "
                "task so the next cycle can fulfill the original request."
            )

    last = tasks[-1]
    if last.get("type") not in (TASK_TYPE_MSG, TASK_TYPE_REPLAN):
        errors.append("Last task must be type 'msg' or 'replan'")

    return errors


# Types that can participate in parallel groups.
_GROUPABLE_TYPES = frozenset({TASK_TYPE_EXEC})


def _validate_plan_groups(tasks: list[dict]) -> list[str]:
    """Validate parallel group constraints.

    Rules:
    - group only on exec (msg/replan → error)
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
                f"Task {i + 1}: group is only allowed on exec tasks, "
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
    force_msg_only: bool = False,
    install_route: dict[str, str] | None = None,
    mcp_methods_pool: dict[str, list] | None = None,
) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid).

    If max_tasks is provided, plans with more tasks are rejected.
    If is_replan is False, extend_replan is stripped.
    If force_msg_only is True, only msg tasks are allowed — all other task
    types are rejected.
    """
    errors, tasks = _validate_plan_structure(plan, max_tasks, is_replan)
    if errors:
        return errors
    if force_msg_only:
        non_msg = [t for t in tasks if t.get("type") != TASK_TYPE_MSG]
        if non_msg:
            errors.append(
                "Plan ONLY msg tasks explaining the situation to the user. "
                "Do NOT plan exec or mcp tasks."
            )
            return errors
    errors.extend(_validate_plan_tasks(
        tasks, installed_skills, installed_skills_info,
        install_approved=install_approved,
        mcp_methods_pool=mcp_methods_pool,
    ))
    errors.extend(_validate_plan_ordering(
        tasks, is_replan, install_approved,
        has_needs_install=bool(plan.get("needs_install")),
        has_knowledge=bool(plan.get("knowledge")),
        has_kb_answer=bool(plan.get("kb_answer")),
        has_awaits_input=bool(plan.get("awaits_input")),
        allow_msg_only=(
            force_msg_only
            or plan.get("msg_only_fallback") == "unavailable_named_tool"
        ),
    ))
    errors.extend(_validate_plan_groups(tasks))

    # goal mentions creating a file/artifact but plan has no exec/mcp task
    goal_words = set(plan.get("goal", "").lower().split())
    has_verb = bool(goal_words & _ARTIFACT_VERBS)
    has_noun = bool(goal_words & _ARTIFACT_NOUNS)
    has_action_task = any(
        t.get("type") == TASK_TYPE_EXEC for t in tasks
    )
    has_needs_install = bool(plan.get("needs_install"))
    if has_verb and has_noun and not has_action_task and not is_replan and not has_needs_install:
        errors.append(
            "Goal mentions creating a file/document but plan has no "
            "exec task. Add an action task that writes the file to the "
            "workspace (e.g. shell redirect, cat, echo). Auto-publish "
            "will generate a download URL automatically."
        )

    # validate knowledge items (if present)
    knowledge = plan.get("knowledge") or []
    for ki, item in enumerate(knowledge, 1):
        if not isinstance(item, str) or len(item.strip()) < _MIN_PROMOTED_FACT_LEN:
            errors.append(
                f"knowledge[{ki}]: must be a string with at least "
                f"{_MIN_PROMOTED_FACT_LEN} characters"
            )

    # needs_install plans are proposal plans — only msg tasks allowed.
    # Execution tasks go in the NEXT plan after the user approves installation.
    if plan.get("needs_install"):
        non_msg = [t["type"] for t in tasks if t.get("type") != TASK_TYPE_MSG]
        if non_msg:
            errors.append(
                f"needs_install is set — only msg tasks are allowed "
                f"(found: {non_msg}). End the plan with a msg asking for approval."
            )
            return errors

    # Bug B coherence check: kb_answer is only valid for msg-only
    # plans answering from briefer-supplied KB context. If the planner
    # sets it together with action tasks, the flag is incoherent — the
    # plan is not actually a recall, it has work to do. Mirrors the
    # needs_install coherence pattern above.
    if plan.get("kb_answer"):
        non_msg = [t["type"] for t in tasks if t.get("type") != TASK_TYPE_MSG]
        if non_msg:
            errors.append(
                f"kb_answer is set but plan contains action tasks "
                f"(found: {non_msg}). kb_answer is only valid for msg-only "
                f"plans answering from briefer-supplied KB context. "
                f"Either remove all action tasks or set kb_answer=false."
            )
            return errors

    # M1579a coherence check: awaits_input means the planner is pausing
    # for user input. A plan that has work to do is by definition not
    # paused. Reject the mismatch — same pattern as kb_answer above.
    if plan.get("awaits_input"):
        non_msg = [t["type"] for t in tasks if t.get("type") != TASK_TYPE_MSG]
        if non_msg:
            errors.append(
                f"awaits_input is set but plan contains action tasks "
                f"(found: {non_msg}). awaits_input is only valid for "
                f"msg-only plans pausing for user input. Either remove "
                f"all action tasks or set awaits_input=false."
            )
            return errors

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
    cats: dict[str, list[str]] = {"project": [], "user": [], "general": []}
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
    for cat in ("project", "user", "general"):
        if cats[cat]:
            parts.append(f"### {cat.title()}\n" + "\n".join(cats[cat]))
    return parts



async def _gather_planner_context(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    paraphrased_context: str | None = None,
) -> PlannerPromptState:
    """Gather planner runtime state before prompt rendering."""
    is_admin = user_role == "admin"
    context_limit = int(config.settings["context_messages"])

    # get_session first — its project_id feeds search_facts
    sess = await get_session(db, session)
    session_project_id = sess["project_id"] if sess else None
    summary = sess["summary"] if sess else ""

    facts, pending, recent = await asyncio.gather(
        search_facts(db, new_message, session=session, is_admin=is_admin, project_id=session_project_id),
        get_pending_items(db, session),
        get_recent_messages(db, session, limit=context_limit),
    )

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

    planner_pack = _build_planner_memory_pack(
        summary=summary,
        facts_text=facts_text,
        pending_text=pending_text,
        recent_text=recent_text,
        paraphrased_context=paraphrased_context,
    )
    from kiso.worker.utils import _build_execution_state
    execution_state = await asyncio.to_thread(_build_execution_state, session)
    context_pool = _merge_context_sections(
        planner_pack.context_sections,
        execution_state.context_sections(),
        owner="planner",
    )

    # Full system_env for briefer context pool (so briefer can decide
    # whether the planner needs OS/binary details for install tasks).
    context_pool["system_env"] = sys_env_full

    # inject available entities for briefer selection, enriched with fact tags
    all_entities = await get_all_entities(db)
    if all_entities:
        # collect fact tags per entity so the briefer knows what each contains
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

    return PlannerPromptState(
        summary=summary,
        facts=facts,
        pending=pending,
        recent=recent,
        memory_pack=planner_pack,
        execution_state=execution_state,
        context_sections=context_pool,
        sys_env_essential=sys_env_essential,
        sys_env_full=sys_env_full,
        install_context=install_ctx,
    )


async def build_planner_messages(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_mcp: str | list[str] | None = None,
    user_skills: str | list[str] | None = None,
    paraphrased_context: str | None = None,
    is_replan: bool = False,
    install_approved: bool = False,
    investigate: bool = False,
    mcp_catalog_text: str | None = None,
    mcp_resources_text: str | None = None,
    mcp_prompts_text: str | None = None,
    out_state: "dict | None" = None,
) -> list[dict]:
    """Build the message list for the planner LLM call.

    Assembles context from session summary, facts, pending questions,
    system environment, skills/MCP catalogs, and recent messages.

    When ``briefer_enabled`` is True in config, calls the briefer LLM to
    select prompt modules and synthesize context. Falls back to full
    context on briefer failure.
    """
    planner_state = await _gather_planner_context(
        db, config, session, user_role, new_message, paraphrased_context,
    )
    summary = planner_state.summary
    facts = planner_state.facts
    pending = planner_state.pending
    recent = planner_state.recent
    context_pool: dict = dict(planner_state.context_sections)
    sys_env_essential = planner_state.sys_env_essential
    sys_env_full = planner_state.sys_env_full
    install_ctx = planner_state.install_context

    # system env doesn't change between plan and replan — exclude from
    # briefer context pool to reduce redundant tokens.
    if is_replan:
        context_pool.pop("system_env", None)

    # Skill discovery — populate context_pool["skills"] so the briefer can
    # select. Metadata-only (name + description + when_to_use); M1540 wires
    # role-scoped skill bodies into planner/worker/reviewer prompts.
    installed_skills = discover_skills()
    # Deterministic activation_hints pre-filter narrows the catalog
    # before the briefer ever sees it. Replan bypasses to avoid filtering
    # out a skill the new plan needs but the original message didn't hint at.
    installed_skills = filter_by_activation_hints(
        installed_skills, new_message, is_replan=is_replan,
    )
    # Per-user skill allowlist — runs after activation_hints so the
    # pre-filter still narrows the superset, then the user-permission
    # layer removes anything the user is not allowed to see.
    if installed_skills:
        from kiso.brain.common import filter_skills_by_user

        names = [getattr(s, "name", None) for s in installed_skills]
        names = [n for n in names if n is not None]
        if names:
            allowed = set(
                filter_skills_by_user(
                    names, role=user_role, allowlist=user_skills,
                )
            )
            installed_skills = [
                s for s in installed_skills
                if getattr(s, "name", None) in allowed
            ]
    if installed_skills:
        skill_lines = []
        for skill in installed_skills:
            meta = metadata_for_briefer(skill)
            line = f"- {meta['name']} — {meta.get('description', '').strip()}"
            if meta.get("when_to_use"):
                line += f" (when: {meta['when_to_use'].strip()})"
            skill_lines.append(line)
        context_pool["skills"] = "\n".join(skill_lines)

    # MCP method catalog — fed to the briefer as a first-class category
    # so the briefer can SELECT MCP methods, not just validate them
    # post-hoc. Caller is responsible for formatting via
    # `format_mcp_catalog(manager)` and passing the result through.
    # When None (no manager in scope), the section is omitted entirely.
    if mcp_catalog_text:
        # M1539: apply per-user MCP method allowlist before the catalog
        # reaches the briefer + planner prompt.
        from kiso.brain.common import filter_mcp_catalog_by_user
        mcp_catalog_text = filter_mcp_catalog_by_user(
            mcp_catalog_text,
            user_role=user_role,
            user_mcp_allow=user_mcp,
        )
        if mcp_catalog_text:
            context_pool["mcp_methods"] = mcp_catalog_text

    # MCP resource catalog — parallel to mcp_methods. Resources are
    # data objects (logs, DB rows, doc pages) the planner can route
    # reads through the synthetic ``__resource_read`` method.
    if mcp_resources_text:
        context_pool["mcp_resources"] = mcp_resources_text

    # MCP prompt catalog — parallel to mcp_methods. Prompts are
    # server-templated instructions fetched via the synthetic
    # ``__prompt_get`` method.
    if mcp_prompts_text:
        context_pool["mcp_prompts"] = mcp_prompts_text

    # Connector discovery — show configured connectors to planner
    connectors = discover_connectors()
    if connectors:
        lines = ["Configured connectors:"]
        for c in connectors:
            lines.append(f"- {c['name']} — {c.get('description', '')}")
        context_pool["connectors"] = "\n".join(lines)

    msg_lower = new_message.lower()

    if "[uploaded files:" in msg_lower:
        context_pool["upload_hint"] = (
            "The user's message references uploaded files. "
            "Use exec tasks (cat, head, python) to read them from the uploads/ directory."
        )

    install_route = _classify_install_mode(
        new_message,
        get_system_env(config),
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

    # session_files module when files exist in workspace
    _has_session_files = "session_files" in context_pool

    if briefing:
        modules = list(briefing["modules"])
        if "skills_and_mcp" not in modules:
            modules.append("skills_and_mcp")
        if _has_session_files and "session_files" not in modules:
            modules.append("session_files")
        if "planning_rules" not in modules:
            modules.append("planning_rules")
        if investigate and "investigate" not in modules:
            modules.append("investigate")
        system_prompt = _load_modular_prompt("planner", modules)
    else:
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
        if _plugin_kw_hit:
            fallback_modules.append("plugin_install")
        if _has_session_files:
            fallback_modules.append("session_files")
        if investigate:
            fallback_modules.append("investigate")
        system_prompt = _load_modular_prompt("planner", fallback_modules)

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
        planner_project_id = await get_session_project_id(db, session)
        scored_facts = await search_facts_scored(
            db,
            entity_id=entity_id,
            tags=briefing.get("relevant_tags") or None,
            keywords=new_message.lower().split()[:10] if new_message else None,
            session=session if not is_admin else None,
            is_admin=is_admin,
            project_id=planner_project_id,
        )
        if scored_facts:
            scored_facts_text = "\n".join(f"- {f['content']}" for f in scored_facts)

    # --- Build context block ---
    context_parts: list[str] = []

    if briefing:
        # Briefer path: use synthesized context + filtered skill/MCP catalog
        _add_section(context_parts, "Context", briefing["context"])
        _add_section(context_parts, "Relevant Facts", scored_facts_text)
        # inject essential system env always (~60 tok). Full version
        # (~400 tok) only when briefer selected install/system modules.
        # Check briefer's raw selection — force-added modules (the
        # skills_and_mcp safety net) don't count since they're added
        # unconditionally.
        _SYSENV_MODULES = {"plugin_install", "kiso_commands", "user_mgmt"}
        _needs_full_sysenv = bool(set(briefing["modules"]) & _SYSENV_MODULES)
        if _needs_full_sysenv:
            context_parts.append(f"## System Environment\n{sys_env_full}")
        else:
            context_parts.append(f"## System Environment\n{sys_env_essential}")
            # when skills_and_mcp is loaded (install-decision rules) but
            # full sysenv isn't warranted, inject just the install-critical
            # fields so the planner can route install commands correctly.
            if "skills_and_mcp" in modules and install_ctx:
                _add_section(context_parts, "Install Context", install_ctx)
        # suppress generic routing when approved — Install Status
        # section (added later) has the authoritative instructions.
        if not install_approved:
            _add_section(context_parts, "Install Routing", install_mode_ctx)
        # inject user-facing settings only when kiso_commands loaded.
        if "kiso_commands" in modules:
            _settings_text = build_user_settings_text(get_system_env(config))
            _add_section(context_parts, "User Settings", _settings_text)
        # Session workspace files + previous plan results — operational data
        # that must reach the planner verbatim (not gated by briefer synthesis).
        _add_context_section(context_parts, context_pool, "session_files", "Session Workspace")
        _add_context_section(context_parts, context_pool, "last_plan", "Previous Plan")
    else:
        # Fallback path: full context dump (original behavior)
        _add_context_section(context_parts, context_pool, "summary", "Session Summary")

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
        if not install_approved:
            _add_section(context_parts, "Install Routing", install_mode_ctx)
        # Session workspace files + previous plan results (same as briefer path)
        _add_context_section(context_parts, context_pool, "session_files", "Session Workspace")
        _add_context_section(context_parts, context_pool, "last_plan", "Previous Plan")

        _add_context_section(context_parts, context_pool, "pending", "Pending Questions")

        if context_pool.get("recent_messages"):
            context_parts.append(
                f"## Recent Messages\n{fence_content(context_pool['recent_messages'], 'MESSAGES')}"
            )

        if context_pool.get("paraphrased"):
            context_parts.append(
                f"## Paraphrased External Messages (untrusted)\n"
                f"{fence_content(context_pool['paraphrased'], 'PARAPHRASED')}"
            )

    # Skills section — M1540: briefer selects by name; planner prompt
    # gets name + description PLUS the skill's `## Planner` body (or the
    # whole body when no role headings are present). When the briefer is
    # bypassed / absent, inject all installed skills.
    if briefing and briefing.get("skills"):
        wanted = set(briefing["skills"])
        selected_skills = [
            s for s in installed_skills
            if (s.name if hasattr(s, "name") else s.get("name")) in wanted
        ]
    else:
        selected_skills = installed_skills
    if out_state is not None:
        out_state["selected_skills"] = list(selected_skills)
    if selected_skills:
        skill_blocks = []
        for skill in selected_skills:
            meta = metadata_for_briefer(skill)
            header = f"### {meta['name']} — {meta.get('description', '').strip()}"
            if meta.get("when_to_use"):
                header += f"\n_When to use: {meta['when_to_use'].strip()}_"
            body = instructions_for_planner(skill).strip()
            block = header + ("\n\n" + body if body else "")
            skill_blocks.append(block)
        context_parts.append(
            "## Skills (planner guidance)\n\n" + "\n\n".join(skill_blocks)
        )

    # MCP method catalog (M1370/M1371) — the briefer received this as
    # selectable input; the planner LLM also needs to see the catalog
    # to actually emit `type=mcp` tasks. Set by build_planner_messages
    # via the `mcp_catalog_text` parameter when a caller has an
    # MCPManager in scope. When empty, the section is omitted entirely
    # and the planner falls back to plain exec routing.
    if context_pool.get("mcp_methods"):
        context_parts.append(
            f"## MCP Methods\n{context_pool['mcp_methods']}"
        )

    if context_pool.get("mcp_resources"):
        context_parts.append(
            f"## MCP Resources\n{context_pool['mcp_resources']}"
        )

    if context_pool.get("mcp_prompts"):
        context_parts.append(
            f"## MCP Prompts\n{context_pool['mcp_prompts']}"
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
            "A prior plan proposed installation and the user approved. "
            "Do NOT set needs_install — the user has already approved. "
            "Plan exec tasks to install directly, then replan as the last task."
        )

    context_parts.append(f"## Caller Role\n{user_role}")
    context_parts.append(f"## New Message\n{fence_content(new_message, 'USER_MSG')}")

    context_block = "\n\n".join(context_parts)

    return _build_messages(system_prompt, context_block)


async def run_planner(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    user_role: str,
    new_message: str,
    user_mcp: str | list[str] | None = None,
    user_skills: str | list[str] | None = None,
    paraphrased_context: str | None = None,
    on_context_ready: Callable | None = None,
    on_retry: Callable[[int, int, str], None] | None = None,
    is_replan: bool = False,
    install_approved: bool = False,
    max_tasks_override: int | None = None,
    investigate: bool = False,
    mcp_manager: "Any | None" = None,
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
        mcp_manager: Optional MCPManager-like object. When provided,
            its cached method catalog is rendered via
            ``format_mcp_catalog`` and injected into the planner
            and briefer context, and its structured pool is used
            for validate_plan's type=mcp validation.

    Returns the validated plan dict with keys: goal, secrets, tasks.
    Raises PlanError if all retries exhausted.
    """
    _mcp_catalog_text = format_mcp_catalog(mcp_manager) if mcp_manager else None
    _mcp_resources_text = (
        format_mcp_resources(mcp_manager) if mcp_manager else None
    )
    _mcp_prompts_text = (
        format_mcp_prompts(mcp_manager) if mcp_manager else None
    )
    planner_out_state: dict = {}
    messages = await build_planner_messages(
        db, config, session, user_role, new_message,
        user_mcp=user_mcp, user_skills=user_skills,
        paraphrased_context=paraphrased_context, is_replan=is_replan,
        install_approved=install_approved, investigate=investigate,
        mcp_catalog_text=_mcp_catalog_text,
        mcp_resources_text=_mcp_resources_text,
        mcp_prompts_text=_mcp_prompts_text,
        out_state=planner_out_state,
    )
    if on_context_ready:
        await on_context_ready()

    max_tasks = max_tasks_override if max_tasks_override is not None else int(config.settings["max_plan_tasks"])

    install_route = _classify_install_mode(new_message, get_system_env(config))

    # inject task budget into planner context so LLM knows the limit.
    budget_line = f"\n\n## Task Budget\nMaximum tasks: {max_tasks}."
    for msg in reversed(messages):
        if msg["role"] == "user":
            msg["content"] += budget_line
            break
    else:
        log.warning("No user message found for budget injection")

    _force_msg = False

    def _validate_plan(p: dict) -> list[str]:
        return validate_plan(
            p, max_tasks=max_tasks, is_replan=is_replan,
            install_approved=install_approved,
            force_msg_only=_force_msg,
            install_route=install_route,
        )

    fallback = config.settings.get("planner_fallback_model") or None
    plan = await _retry_llm_with_validation(
        config, "planner", messages, PLAN_SCHEMA,
        _validate_plan,
        PlanError, "Plan",
        session=session,
        on_retry=on_retry,
        fallback_model=fallback,
    )
    plan["install_proposal"] = bool(plan.get("needs_install"))
    plan["_selected_skills"] = planner_out_state.get("selected_skills", [])

    log.info("Plan: goal=%r, %d tasks, install_proposal=%s",
             plan["goal"], len(plan["tasks"]), plan["install_proposal"])
    return plan


__brain_exports__ = [
    name
    for name in globals()
    if name not in _IMPORTED_NAMES and name not in {"__brain_exports__", "_IMPORTED_NAMES"}
]

del _IMPORTED_NAMES
