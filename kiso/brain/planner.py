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
from kiso.registry import get_registry_wrappers
from kiso.recipe_loader import (
    build_planner_recipe_list,
    discover_recipes,
    filter_recipes_for_message,
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
from kiso.wrappers import (
    build_planner_wrapper_list,
    discover_wrappers,
    validate_wrapper_args,
    validate_wrapper_args_semantic,
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
    TASK_TYPE_SEARCH,
    TASK_TYPE_WRAPPER,
    TASK_TYPES,
    _INSTALL_CMD_RE,
    _INSTALL_MODE_KISO_WRAPPER,
    _INSTALL_MODE_NONE,
    _INSTALL_MODE_UNKNOWN_KISO_WRAPPER,
    _INSTALL_NAME_RE,
    _MIN_PROMOTED_FACT_LEN,
    _NPM_GLOBAL_RE,
    _NPX_RE,
    _PIP_INSTALL_RE,
    _SYSTEM_INSTALL_HINT_RE,
    _TYPE_EXAMPLES,
    _WRAPPER_UNAVAILABLE_MARKER,
    _UV_PIP_RE,
    _VALID_FACT_CATEGORIES,
    _add_context_section,
    _add_section,
    _build_install_mode_context,
    _build_messages,
    _build_planner_memory_pack,
    _classify_install_mode,
    _format_pending_items,
    _is_plugin_discovery_search,
    _join_or_empty,
    _load_modular_prompt,
    _merge_context_sections,
    _normalize_install_target_token,
    _parse_registry_hint_names,
    _prefilter_context_pool,
    _retry_llm_with_validation,
    _GIT_URL_RE,
    build_recent_context,
    run_briefer,
)

if TYPE_CHECKING:
    from kiso.worker.utils import ExecutionState

log = logging.getLogger("kiso.brain")

_IMPORTED_NAMES = set(globals())

_KISO_CMD_KEYWORDS = frozenset({"wrapper", "connector", "env", "instance", "kiso"})
_USER_MGMT_KEYWORDS = frozenset({"user", "admin", "alias"})

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


def _find_direct_wrapper_exec(
    detail: str, installed_skills: list[str] | None,
) -> str | None:
    """Return wrapper name if exec detail tries to use an installed kiso wrapper.

    This is intentionally narrow: it catches routing mistakes like
    "Use aider to write ..." or "Run browser on https://...".
    Normal shell tasks mentioning unrelated words must keep passing.
    """
    if not installed_skills:
        return None

    lower = (detail or "").lower()
    if "kiso wrapper " in lower or "kiso connector " in lower:
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
    mcp_methods_pool: dict[str, list] | None = None,
) -> list[str]:
    """Check per-task rules: type, detail, expect, args, wrapper validation."""
    errors: list[str] = []
    replan_count = 0
    for i, task in enumerate(tasks, 1):
        t = task.get("type")
        if t not in TASK_TYPES:
            errors.append(f"Task {i}: unknown type {t!r}")
            continue
        if t in (TASK_TYPE_EXEC, TASK_TYPE_WRAPPER, TASK_TYPE_SEARCH, TASK_TYPE_MCP) and task.get("expect") is None:
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
                f"rewrite as a concrete shell command description "
                f"(e.g., 'Run kiso wrapper install browser')"
            )
        direct_tool_exec = _find_direct_wrapper_exec(detail, installed_skills)
        if t == TASK_TYPE_EXEC and direct_tool_exec:
            errors.append(
                f"Task {i}: exec detail directly routes installed wrapper '{direct_tool_exec}'. "
                f"Installed kiso wrappers must use type='wrapper' with wrapper='{direct_tool_exec}', "
                f"not type='exec'."
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
        if t in (TASK_TYPE_EXEC, TASK_TYPE_WRAPPER, TASK_TYPE_SEARCH) and _mentions_user_delivery(detail):
            errors.append(
                f"Task {i}: action task detail includes user-delivery wording. "
                f"Action tasks should do the work only; use a final msg task "
                f"to tell/send results to the user."
            )
        # kiso plugin install for names not in registry (without git URL)
        if t == TASK_TYPE_EXEC and registry_hint_names is not None:
            name_match = _INSTALL_NAME_RE.search(detail)
            if name_match and not _GIT_URL_RE.search(detail):
                install_name = _normalize_install_target_token(name_match.group(1))
                if not install_name:
                    continue
                if install_name not in registry_hint_names:
                    errors.append(
                        f"Task {i}: '{install_name}' is not in the kiso plugin registry. "
                        f"For system packages use the package manager (e.g. apt-get install), "
                        f"for Python libraries use uv pip install."
                    )
        if t == TASK_TYPE_MSG:
            for field in ("expect", "wrapper", "args"):
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
                    "If the wrapper name appears in registry_hints or "
                    "'Available Tools (not installed)', it is a kiso wrapper — "
                    "use the kiso_native install flow (set needs_install, "
                    "msg for approval). If it does NOT appear there, it is "
                    "not a kiso plugin — for system packages use the package "
                    "manager (e.g. apt-get install), for Python libraries "
                    "use uv pip install."
                )
            if task.get("wrapper") is not None:
                errors.append(f"Task {i}: search task must have wrapper = null")
        if t == TASK_TYPE_REPLAN:
            replan_count += 1
            if task.get("expect") is not None:
                errors.append(f"Task {i}: replan task must have expect = null")
            if task.get("wrapper") is not None:
                errors.append(f"Task {i}: replan task must have wrapper = null")
            if task.get("args") is not None:
                errors.append(f"Task {i}: replan task must have args = null")
            if i != len(tasks):
                errors.append(f"Task {i}: replan task can only be the last task")
        if t == TASK_TYPE_WRAPPER:
            wrapper_name = task.get("wrapper")
            if not wrapper_name:
                errors.append(f"Task {i}: wrapper task must have a non-null wrapper name")
            elif wrapper_name in (TASK_TYPE_EXEC, TASK_TYPE_MSG, TASK_TYPE_REPLAN):
                errors.append(
                    f"Task {i}: '{wrapper_name}' is a task TYPE, not a wrapper. "
                    f"Use type='{wrapper_name}' instead of type='wrapper' with "
                    f"wrapper='{wrapper_name}'."
                )
            elif wrapper_name in BRIEFER_MODULES:
                errors.append(
                    f"Task {i}: '{wrapper_name}' is a prompt module, not a wrapper. "
                    f"For shell commands, use type='exec'. For installed wrappers, "
                    f"use type='wrapper' with an actual wrapper name from the available list."
                )
            elif installed_skills is not None and wrapper_name not in installed_skills:
                available = ", ".join(sorted(installed_skills)) if installed_skills else "none"
                if install_approved:
                    errors.append(
                        f"Task {i}: wrapper '{wrapper_name}' is not installed. "
                        f"Available wrappers: {available}. "
                        f"You CANNOT use type=wrapper for uninstalled wrappers. "
                        f"Installation is approved — plan an exec task to install "
                        f"{wrapper_name} via the kiso CLI, then replan to use it."
                    )
                elif registry_hint_names and wrapper_name in registry_hint_names:
                    errors.append(
                        f"Task {i}: wrapper '{wrapper_name}' is not installed but IS "
                        f"available in the registry. If a built-in task type "
                        f"(e.g. search) can achieve the same goal, use that "
                        f"instead. Otherwise, plan a SINGLE msg task asking "
                        f"whether to install '{wrapper_name}', then end the plan."
                    )
                else:
                    errors.append(
                        f"Task {i}: wrapper '{wrapper_name}' is "
                        f"{_WRAPPER_UNAVAILABLE_MARKER}. Plan a SINGLE msg task "
                        f"informing the user that '{wrapper_name}' cannot be found "
                        f"in the public registry. If the user may have a private "
                        f"source, suggest providing a git URL or installation "
                        f"instructions. Do NOT plan any exec, search, or wrapper "
                        f"tasks referencing this wrapper."
                    )
            elif installed_skills_info and wrapper_name in installed_skills_info:
                args_raw = task.get("args") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except (json.JSONDecodeError, TypeError):
                    errors.append(
                        f"Task {i}: wrapper args must be a JSON object with named fields"
                    )
                else:
                    if not isinstance(args, dict):
                        errors.append(
                            f"Task {i}: wrapper args must be a JSON object with named fields"
                        )
                        continue
                    schema = installed_skills_info[wrapper_name].get("args_schema", {})
                    arg_errors = validate_wrapper_args(args, schema)
                    semantic_errors = validate_wrapper_args_semantic(
                        installed_skills_info[wrapper_name],
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
                        # show only required args in example so the
                        # model focuses on what it MUST provide.
                        required_args = {
                            aname: _TYPE_EXAMPLES.get(adef.get("type", "string"), "value")
                            for aname, adef in schema.items()
                            if adef.get("required", False)
                        }
                        example_json = json.dumps(required_args)
                        errors.append(
                            f"Task {i}: wrapper '{wrapper_name}' args invalid: "
                            + "; ".join(arg_errors)
                            + f". Required args object: '{example_json}'"
                        )
                    # browser must use web URLs, not local file paths
                    if wrapper_name == "browser":
                        for v in args.values():
                            if isinstance(v, str) and v.startswith("file://"):
                                errors.append(
                                    f"Task {i}: browser cannot open local files "
                                    f"(file:// URL). Use exec with cat/head to "
                                    f"read local files instead."
                                )
                                break
        if t == TASK_TYPE_MCP:
            server = task.get("server")
            method = task.get("method")
            if task.get("wrapper") is not None:
                errors.append(
                    f"Task {i}: mcp task must have wrapper=null"
                )
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
            if (
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
                        # Validate args against the method's inputSchema
                        # via jsonschema. Failure yields a precise error
                        # naming the offending property.
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
                        if args_dict is not None and target.input_schema:
                            try:
                                import jsonschema  # runtime dep
                                jsonschema.validate(
                                    instance=args_dict,
                                    schema=target.input_schema,
                                )
                            except jsonschema.ValidationError as e:
                                # Drop newlines so the retry feedback stays
                                # on a single line.
                                msg = str(e).replace("\n", " ")
                                errors.append(
                                    f"Task {i}: mcp args invalid against "
                                    f"{server}:{method} inputSchema: {msg}"
                                )
                            except Exception as e:  # noqa: BLE001
                                errors.append(
                                    f"Task {i}: mcp args schema validation "
                                    f"failed: {e}"
                                )

    if replan_count > 1:
        errors.append("A plan can have at most one replan task")

    return errors


# goal-plan mismatch — detect artifact requests with no exec/wrapper task.
_ARTIFACT_VERBS = frozenset({"create", "write", "generate", "build", "produce", "make"})
_ARTIFACT_NOUNS = frozenset({
    "file", "document", "script", "markdown", "csv", "report",
    "table", "spreadsheet", "config", "template", "page",
})


_GOAL_RUN_KEYWORDS = frozenset({"run", "test", "execute", "launch", "start"})


def _validate_plan_ordering(
    tasks: list[dict], is_replan: bool, install_approved: bool,
    has_needs_install: bool = False,
    has_knowledge: bool = False,
    has_kb_answer: bool = False,
    allow_msg_only: bool = False,
    goal: str = "",
) -> list[str]:
    """Check cross-task ordering rules and install safety."""
    errors: list[str] = []

    # msg-only plans are rejected unless one of the escape flags is set:
    # needs_install (install proposal), knowledge (storage), kb_answer
    # (: KB recall from briefer context), or allow_msg_only
    # (structural fallback).
    _DATA_TYPES = {TASK_TYPE_EXEC, TASK_TYPE_SEARCH, TASK_TYPE_WRAPPER, TASK_TYPE_REPLAN, TASK_TYPE_MCP}
    has_action = any(t.get("type") in _DATA_TYPES for t in tasks)
    if not has_action and not is_replan:
        if (
            not has_needs_install
            and not has_knowledge
            and not has_kb_answer
            and not allow_msg_only
        ):
            errors.append(
                "Plan has only msg tasks — include at least one "
                "exec/wrapper/search task for action requests. "
                "Msg-only is valid only for kiso wrapper install proposals "
                "(set needs_install), knowledge storage, or KB recall "
                "(set kb_answer when answering from briefer context)."
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
                f"Task {first_install_idx + 1}: installs a wrapper/connector in the first plan. "
                f"You CANNOT install in the same plan that asks for permission — the user "
                f"hasn't replied yet. Plan a SINGLE msg task asking whether to install, "
                f"offer alternatives, and end the plan there. The install happens in the "
                f"next cycle after the user approves."
            )

    # after installing a wrapper that was proposed in a prior turn, the
    # original request is still pending — must replan to continue with it.
    if install_approved:
        has_install_exec = any(
            t.get("type") == TASK_TYPE_EXEC
            and _INSTALL_CMD_RE.search(t.get("detail", ""))
            for t in tasks
        )
        if has_install_exec and tasks[-1].get("type") == TASK_TYPE_MSG:
            errors.append(
                "Plan installs a wrapper after user approval but ends with msg. "
                "The original request is still pending — use replan as the last "
                "task so the next cycle can fulfill the original request."
            )

    last = tasks[-1]
    if last.get("type") not in (TASK_TYPE_MSG, TASK_TYPE_REPLAN):
        errors.append("Last task must be type 'msg' or 'replan'")

    # codegen-only pattern = plan starts with wrapper then exec.
    # Only fire when tasks[0]=wrapper, tasks[1]=exec.  Multi-step workflows
    # where wrapper comes after other tasks are intentional, not verification.
    if (
        len(tasks) >= 2
        and tasks[0].get("type") == TASK_TYPE_WRAPPER
        and tasks[1].get("type") == TASK_TYPE_EXEC
        and not bool(set(goal.lower().split()) & _GOAL_RUN_KEYWORDS)
    ):
        errors.append(
            "Task 2: exec immediately after wrapper — reviewer already "
            "inspects wrapper output. Remove the exec task. Add exec after wrapper "
            "ONLY when the user asks to run or test the result."
        )

    return errors


def _validate_install_route_consistency(
    plan: dict,
    tasks: list[dict],
    install_route: dict[str, str] | None,
    *,
    install_approved: bool,
) -> list[str]:
    """Validate that the plan stays consistent with the deterministic install route."""
    if not install_route:
        return []

    mode = install_route.get("mode", _INSTALL_MODE_NONE)
    target = (install_route.get("target") or "").lower()
    if mode == _INSTALL_MODE_NONE or not target:
        return []

    errors: list[str] = []
    exec_details = [
        (i, (t.get("detail") or ""))
        for i, t in enumerate(tasks, 1)
        if t.get("type") == TASK_TYPE_EXEC
    ]
    non_msg_types = [t.get("type") for t in tasks if t.get("type") != TASK_TYPE_MSG]

    if mode == _INSTALL_MODE_UNKNOWN_KISO_WRAPPER:
        if plan.get("needs_install"):
            errors.append(
                f"Unknown named wrapper '{target}' is not in the installed/registry context. "
                f"Do NOT set needs_install."
            )
        if non_msg_types:
            errors.append(
                f"Unknown named wrapper '{target}' is not available in the current kiso wrapper context. "
                f"Plan ONLY msg tasks explaining that it cannot be installed from the current registry/wrapper set, "
                f"and suggest a git URL or private installation instructions if applicable."
            )
        return errors

    if mode != _INSTALL_MODE_KISO_WRAPPER or install_route.get("target_installed"):
        return errors

    has_target_mention_install = False
    for i, detail in exec_details:
        lower = detail.lower()
        name_match = _INSTALL_NAME_RE.search(detail)
        if name_match:
            install_name = _normalize_install_target_token(name_match.group(1))
            if not install_name:
                continue
            if install_name == target:
                has_target_mention_install = True
                continue
            errors.append(
                f"Task {i}: install routing target is '{target}', but this plan installs '{install_name}'. "
                f"Use `kiso wrapper install {target}` for the approved registry wrapper."
            )
            continue
        if _SYSTEM_INSTALL_HINT_RE.search(lower) or _PIP_INSTALL_RE.search(lower):
            errors.append(
                f"Task {i}: install routing target '{target}' is a kiso wrapper. "
                f"Do not use system package managers or pip/uv pip here; use `kiso wrapper install {target}`."
            )
            continue
        # Natural-language detail mentioning the target + install intent
        if target in lower and "install" in lower:
            has_target_mention_install = True

    explicit_request = install_route.get("explicit_install_request", False)

    if install_approved and not has_target_mention_install:
        errors.append(
            f"Approved install for kiso wrapper '{target}' requires an exec task that "
            f"installs '{target}', then a final replan task."
        )

    if not install_approved and not plan.get("needs_install") and non_msg_types:
        if explicit_request and has_target_mention_install:
            if tasks and tasks[-1].get("type") == TASK_TYPE_MSG:
                errors.append(
                    f"Explicit install for kiso wrapper '{target}' must end with replan, "
                    f"not msg — the original request may still be pending."
                )
        else:
            errors.append(
                f"Known registry wrapper '{target}' is not installed yet. "
                f"Before approval, propose installation with needs_install + msg only."
            )

    return errors


# Types that can participate in parallel groups.
_GROUPABLE_TYPES = frozenset({TASK_TYPE_EXEC, TASK_TYPE_SEARCH, TASK_TYPE_WRAPPER})


def _validate_plan_groups(tasks: list[dict]) -> list[str]:
    """Validate parallel group constraints.

    Rules:
    - group only on exec/search/wrapper (msg/replan → error)
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
                f"Task {i + 1}: group is only allowed on exec/search/wrapper tasks, "
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
    install_route: dict[str, str] | None = None,
    mcp_methods_pool: dict[str, list] | None = None,
) -> list[str]:
    """Validate plan semantics. Returns list of error strings (empty = valid).

    If installed_skills is provided, wrapper tasks are validated against it.
    If max_tasks is provided, plans with more tasks are rejected.
    If installed_skills_info is provided (name→wrapper dict), wrapper args are
    validated against the schema at plan time.
    If is_replan is False, extend_replan is stripped.
    If registry_hint_names is provided, exec tasks with ``kiso wrapper install``
    are validated: the name must be in the registry (or a git URL).
    If force_msg_only is True, only msg tasks are allowed — all other task
    types are rejected (set after a wrapper-not-in-registry rejection).
    """
    errors, tasks = _validate_plan_structure(plan, max_tasks, is_replan)
    if errors:
        return errors
    # after a wrapper was determined to not exist in any registry,
    # force the planner to produce a msg-only plan.
    if force_msg_only:
        non_msg = [t for t in tasks if t.get("type") != TASK_TYPE_MSG]
        if non_msg:
            errors.append(
                "The requested wrapper does not exist in any registry. "
                "Plan ONLY msg tasks explaining the situation to the user. "
                "Do NOT plan exec, wrapper, or search tasks."
            )
            return errors
    errors.extend(_validate_plan_tasks(
        tasks, installed_skills, installed_skills_info,
        install_approved=install_approved,
        registry_hint_names=registry_hint_names,
        mcp_methods_pool=mcp_methods_pool,
    ))
    errors.extend(_validate_plan_ordering(
        tasks, is_replan, install_approved,
        has_needs_install=bool(plan.get("needs_install")),
        has_knowledge=bool(plan.get("knowledge")),
        has_kb_answer=bool(plan.get("kb_answer")),
        allow_msg_only=(
            force_msg_only
            or plan.get("msg_only_fallback") == "unavailable_named_tool"
            or (
                bool(install_route)
                and install_route.get("mode") == _INSTALL_MODE_UNKNOWN_KISO_WRAPPER
            )
        ),
        goal=plan.get("goal", ""),
    ))
    errors.extend(_validate_install_route_consistency(
        plan, tasks, install_route, install_approved=install_approved,
    ))
    errors.extend(_validate_plan_groups(tasks))

    # goal mentions creating a file/artifact but plan has no exec/wrapper task
    goal_words = set(plan.get("goal", "").lower().split())
    has_verb = bool(goal_words & _ARTIFACT_VERBS)
    has_noun = bool(goal_words & _ARTIFACT_NOUNS)
    has_action_task = any(
        t.get("type") in (TASK_TYPE_EXEC, TASK_TYPE_WRAPPER) for t in tasks
    )
    has_needs_install = bool(plan.get("needs_install"))
    if has_verb and has_noun and not has_action_task and not is_replan and not has_needs_install:
        errors.append(
            "Goal mentions creating a file/document but plan has no "
            "exec or wrapper task. Add an action task that writes the "
            "file to the workspace — either an exec task (e.g. shell "
            "redirect, cat, echo) or a wrapper task whose wrapper "
            "produces files (e.g. aider, datagen). Auto-publish will "
            "generate a download URL automatically."
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
            # When the only drafted non-msg tasks are `search`, bias
            # the retry toward dropping `needs_install` instead of
            # reducing to msg-only: `search` is a built-in Kiso
            # capability that never requires a wrapper install, so
            # proposing an install alongside it is semantically wrong.
            if non_msg and all(t == TASK_TYPE_SEARCH for t in non_msg):
                errors.append(
                    f"needs_install is set but the plan uses `search` "
                    f"(found: {non_msg}). `search` is a built-in Kiso "
                    f"capability that does not require any wrapper "
                    f"install. Drop needs_install and keep the search "
                    f"task(s) — the built-in handles this request "
                    f"without installing anything."
                )
            else:
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

    # coherence — wrappers listed in needs_install must not appear in wrapper tasks
    needs = plan.get("needs_install") or []
    if needs:
        for i, t in enumerate(tasks, 1):
            if t.get("type") == TASK_TYPE_WRAPPER and t.get("wrapper") in needs:
                if install_approved:
                    errors.append(
                        f"Task {i}: wrapper '{t['wrapper']}' is not installed yet. "
                        f"Install is approved — plan ONLY an exec task to install "
                        f"{t['wrapper']} via the kiso CLI, then replan as last task. "
                        f"Wrapper tasks go in the NEXT plan after install completes."
                    )
                else:
                    errors.append(
                        f"Task {i}: wrapper '{t['wrapper']}' is in needs_install (not "
                        f"available) but used as a wrapper task. Plan a msg asking "
                        f"to install, then end the plan. The wrapper task goes in a "
                        f"future plan after the user approves and the wrapper is installed."
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
    cats: dict[str, list[str]] = {"project": [], "user": [], "wrapper": [], "general": []}
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
    for cat in ("project", "user", "wrapper", "general"):
        if cats[cat]:
            parts.append(f"### {cat.title()}\n" + "\n".join(cats[cat]))
    return parts


# Capability keywords → wrapper name they require.  Used by the
# capability-gap heuristic to inject plugin-install guidance when the
# message implies a capability not covered by installed wrappers.
# Keep minimal — only precise keywords that unambiguously require a wrapper.
_KISO_CMD_KEYWORDS = frozenset({"wrapper", "connector", "env", "instance", "kiso"})
_USER_MGMT_KEYWORDS = frozenset({"user", "admin", "alias"})



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

    # inject recipes into context pool
    recipes = filter_recipes_for_message(discover_recipes(), new_message)
    recipes_text = build_planner_recipe_list(recipes)
    if recipes_text:
        context_pool["recipes"] = recipes_text
        context_pool["_raw_recipes"] = recipes

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
    user_wrappers: str | list[str] | None = None,
    paraphrased_context: str | None = None,
    is_replan: bool = False,
    install_approved: bool = False,
    investigate: bool = False,
    mcp_catalog_text: str | None = None,
) -> tuple[list[dict], list[str], list[dict]]:
    """Build the message list for the planner LLM call.

    Assembles context from session summary, facts, pending questions,
    system environment, wrappers, and recent messages.

    When ``briefer_enabled`` is True in config, calls the briefer LLM to
    select prompt modules, filter wrappers, and synthesize context. Falls
    back to full context on briefer failure.

    Returns (messages, installed_wrapper_names, installed_wrappers_info) — the
    caller can reuse the wrapper names list for plan validation and the
    tools_info list for args validation without rescanning the filesystem.
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

    # Wrapper discovery — rescan on each planner call
    installed = discover_wrappers()
    installed_names = [s["name"] for s in installed]
    if installed_names:
        log.info("discover_wrappers() found: %s", ", ".join(installed_names))

    # Build the wrapper list text for context pool
    full_wrapper_list = build_planner_wrapper_list(installed, user_role, user_wrappers)
    if full_wrapper_list:
        context_pool["wrappers"] = full_wrapper_list

    # MCP method catalog — fed to the briefer as a first-class category
    # so the briefer can SELECT MCP methods, not just validate them
    # post-hoc. Caller is responsible for formatting via
    # `format_mcp_catalog(manager)` and passing the result through.
    # When None (no manager in scope), the section is omitted entirely.
    if mcp_catalog_text:
        context_pool["mcp_methods"] = mcp_catalog_text

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

    # --- Registry: show available-but-not-installed wrappers ---
    # Show uninstalled registry wrappers so the planner knows what's available
    # for install.  Filtered by installed_names, so returns empty when all
    # wrappers are installed.  Skip on replans — wrappers won't change mid-replan.
    registry_text = ""
    if not is_replan:
        registry_text = await asyncio.to_thread(
            get_registry_wrappers, set(installed_names),
        )
    install_route = _classify_install_mode(
        new_message,
        get_system_env(config),
        installed_wrapper_names=installed_names,
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

    # session_files module when files exist in workspace
    _has_session_files = "session_files" in context_pool

    if briefing:
        # Briefer path: modules selected by the briefer LLM.
        # Safety net: force kiso_native (install decision rules) when no wrappers.
        # Note: plugin_install NOT forced here — it has "curl registry" advice
        # that conflicts with the core "not in hints → apt-get" rule. The
        # briefer selects plugin_install when actually needed.
        modules = list(briefing["modules"])
        if not installed or registry_text:
            if "kiso_native" not in modules:
                modules.append("kiso_native")
        if _has_session_files and "session_files" not in modules:
            modules.append("session_files")
        # planning_rules contains fundamental task-ordering and
        # expect rules that must always be present (matches fallback path).
        if "planning_rules" not in modules:
            modules.append("planning_rules")
        # wrappers_rules needed when any wrappers are installed — contains
        # "use directly" rule and args/guide validation.  Broader than
        # (which checked briefing["wrappers"]) because the briefer sometimes
        # skips wrapper selection even when wrappers are relevant.
        if installed and "wrappers_rules" not in modules:
            modules.append("wrappers_rules")
        # investigate mode injects the read-only diagnose-first
        # contract into the planner system prompt.
        if investigate and "investigate" not in modules:
            modules.append("investigate")
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
        # investigate mode injects the read-only diagnose-first
        # contract into the planner system prompt.
        if investigate:
            fallback_modules.append("investigate")
        system_prompt = _load_modular_prompt("planner", fallback_modules)

    if not installed:
        log.warning("discover_wrappers() returned empty — no wrappers available for planner")

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
        # Briefer path: use synthesized context + filtered wrappers
        _add_section(context_parts, "Context", briefing["context"])
        _add_section(context_parts, "Relevant Facts", scored_facts_text)
        # inject essential system env always (~60 tok). Full version
        # (~400 tok) only when briefer selected install/system modules.
        # Check briefer's raw selection — force-added modules (kiso_native
        # safety net) don't count since they're added unconditionally.
        _SYSENV_MODULES = {"plugin_install", "kiso_commands", "user_mgmt"}
        _needs_full_sysenv = bool(set(briefing["modules"]) & _SYSENV_MODULES)
        if _needs_full_sysenv:
            context_parts.append(f"## System Environment\n{sys_env_full}")
        else:
            context_parts.append(f"## System Environment\n{sys_env_essential}")
            # when kiso_native is loaded (install-decision rules) but
            # full sysenv isn't warranted, inject just the install-critical
            # fields so the planner can route install commands correctly.
            if "kiso_native" in modules and install_ctx:
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
    # skip briefer wrapper filtering when few wrappers installed — marginal
    # token saving vs catastrophic risk of excluding the right wrapper.
    tool_filter_threshold = setting_int(
        config.settings, "briefer_wrapper_filter_threshold", lo=0,
    )
    if briefing and briefing["wrappers"]:
        if len(installed) <= tool_filter_threshold:
            # Few wrappers — inject all but with guides only for selected wrappers
            log.debug("Skipping briefer wrapper filter: %d wrappers <= threshold %d",
                      len(installed), tool_filter_threshold)
            _selected = set(briefing["wrappers"])
            tiered_list = build_planner_wrapper_list(installed, user_role, user_wrappers, selected_names=_selected)
            if tiered_list:
                context_parts.append(f"## Wrappers\n{tiered_list}")
        else:
            selected_names = set(briefing["wrappers"])
            selected_tools = [t for t in installed if t["name"] in selected_names]
            selected_tool_text = build_planner_wrapper_list(selected_tools, user_role, user_wrappers)
            if selected_tool_text:
                context_parts.append(f"## Wrappers\n{selected_tool_text}")
    elif full_wrapper_list:
        context_parts.append(f"## Wrappers\n{full_wrapper_list}")

    # warn planner when web module is active but browser isn't installed.
    # Emphasise that built-in search works without any wrapper for research queries.
    if "web" in (modules if briefing else fallback_modules) and "browser" not in installed_names:
        context_parts.append(
            "## Browser Availability\n"
            "The browser wrapper is NOT installed. "
            "For web research and reading page content, use the built-in `search` task type — "
            "it requires no wrapper and works immediately. "
            "The browser wrapper is only needed for interactive browsing (navigate to a specific URL, "
            "click, fill forms, take screenshots). "
            "If interactive browsing is required: single msg asking to install, end plan.\n"
            "Note: if the user also asks to create/write a file, an exec task is still "
            "required — search alone cannot create files."
        )

    # always-inject available registry wrappers (not gated by briefer) so the
    # planner knows what wrappers can be installed via `kiso wrapper install`.
    if registry_text:
        context_parts.append(f"## Available wrappers (not installed)\n{registry_text}")

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
            "A prior plan proposed wrapper installation and the user approved. "
            "Do NOT set needs_install — the user has already approved. "
            "Plan exec tasks to install directly via the kiso CLI "
            "(e.g., exec 'kiso wrapper install browser'), then replan as last task. "
            "Do NOT add wrapper tasks for uninstalled wrappers — they become "
            "available after the replan. "
            "For wrappers already installed: use them directly."
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
    user_wrappers: str | list[str] | None = None,
    paraphrased_context: str | None = None,
    on_context_ready: Callable | None = None,
    on_retry: Callable[[int, int, str], None] | None = None,
    is_replan: bool = False,
    install_approved: bool = False,
    max_tasks_override: int | None = None,
    investigate: bool = False,
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
        db, config, session, user_role, new_message, user_wrappers=user_wrappers,
        paraphrased_context=paraphrased_context, is_replan=is_replan,
        install_approved=install_approved, investigate=investigate,
    )
    if on_context_ready:
        await on_context_ready()
    tools_by_name = {s["name"]: s for s in installed_info}

    max_tasks = max_tasks_override if max_tasks_override is not None else int(config.settings["max_plan_tasks"])

    # Extract registry hint names for install validation.
    _sysenv = get_system_env(config)
    _reg_hint_names = _parse_registry_hint_names(_sysenv.get("registry_hints", ""))
    install_route = _classify_install_mode(
        new_message,
        _sysenv,
        installed_wrapper_names=installed_names,
        registry_hint_names=_reg_hint_names,
    )

    # inject task budget into planner context so LLM knows the limit.
    budget_line = f"\n\n## Task Budget\nMaximum tasks: {max_tasks}."
    for msg in reversed(messages):
        if msg["role"] == "user":
            msg["content"] += budget_line
            break
    else:
        log.warning("No user message found for budget injection")

    # track whether a wrapper was rejected as not-in-registry across
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
            install_route=install_route,
        )
        if any(_WRAPPER_UNAVAILABLE_MARKER in e for e in errs):
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
    # detect install proposal from two sources:
    # 1. Planner explicitly declared needs_install (preferred, direct)
    # 2. Validation saw uninstalled-wrapper errors (backup, indirect)
    saw_uninstalled = plan.pop("_saw_uninstalled_wrapper", False)
    tasks = plan.get("tasks") or []
    if (
        tasks
        and all(t.get("type") == TASK_TYPE_MSG for t in tasks)
        and (
            _force_msg
            or install_route.get("mode") == _INSTALL_MODE_UNKNOWN_KISO_WRAPPER
        )
    ):
        plan["msg_only_fallback"] = "unavailable_named_tool"

    # Filter needs_install: remove wrappers that are already installed.
    # The LLM sometimes lists installed wrappers in needs_install by mistake.
    needs = plan.get("needs_install") or []
    if needs and installed_names:
        needs = [n for n in needs if n not in installed_names]
        plan["needs_install"] = needs or None

    plan["install_proposal"] = (
        bool(plan.get("needs_install"))
        or saw_uninstalled
    )

    log.info("Plan: goal=%r, %d tasks, install_proposal=%s",
             plan["goal"], len(plan["tasks"]), plan["install_proposal"])
    return plan


__brain_exports__ = [
    name
    for name in globals()
    if name not in _IMPORTED_NAMES and name not in {"__brain_exports__", "_IMPORTED_NAMES"}
]

del _IMPORTED_NAMES
