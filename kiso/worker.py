"""Per-session asyncio worker — drains queue, plans, executes tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
from pathlib import Path

import aiosqlite

from kiso.config import ConfigError, reload_config
from kiso.security import (
    check_command_deny_list,
    collect_deploy_secrets,
    fence_content,
    revalidate_permissions,
    sanitize_output,
)
from kiso.brain import (
    CuratorError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    run_curator,
    run_fact_consolidation,
    run_paraphraser,
    run_planner,
    run_reviewer,
    run_summarizer,
)
from kiso.config import Config, KISO_DIR
from kiso.llm import LLMError, call_llm
from kiso.skills import (
    SkillError,
    build_skill_env,
    build_skill_input,
    discover_skills,
    validate_skill_args,
)
from kiso.webhook import deliver_webhook
from kiso.store import (
    count_messages,
    create_plan,
    create_task,
    delete_facts,
    get_facts,
    get_oldest_messages,
    get_pending_learnings,
    get_session,
    get_tasks_for_plan,
    get_untrusted_messages,
    mark_message_processed,
    save_fact,
    save_learning,
    save_message,
    save_pending_item,
    update_learning,
    update_plan_status,
    update_summary,
    update_task,
)

log = logging.getLogger(__name__)


def _default_worker_prompt() -> str:
    return """\
You are a helpful assistant. Given a task description, produce a clear and
concise response for the user. Use only the information provided in the
task detail and context. Do not invent information.
"""


def _load_worker_prompt() -> str:
    path = KISO_DIR / "roles" / "worker.md"
    if path.exists():
        return path.read_text()
    return _default_worker_prompt()


def _session_workspace(session: str) -> Path:
    """Return and ensure the session workspace directory exists."""
    workspace = KISO_DIR / "sessions" / session
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _write_plan_outputs(session: str, plan_outputs: list[dict]) -> None:
    """Write plan_outputs.json to the session workspace's .kiso/ directory."""
    workspace = _session_workspace(session)
    kiso_dir = workspace / ".kiso"
    kiso_dir.mkdir(exist_ok=True)
    (kiso_dir / "plan_outputs.json").write_text(
        json.dumps(plan_outputs, indent=2, ensure_ascii=False)
    )


def _cleanup_plan_outputs(session: str) -> None:
    """Remove plan_outputs.json after plan completion."""
    workspace = _session_workspace(session)
    outputs_file = workspace / ".kiso" / "plan_outputs.json"
    if outputs_file.exists():
        outputs_file.unlink()


def _resolve_sandbox_uid(config: Config) -> int | None:
    """Resolve sandbox user UID. Returns None if disabled or user not found."""
    if not config.settings.get("sandbox_enabled", False):
        return None
    sandbox_user = str(config.settings.get("sandbox_user", "kiso-sandbox"))
    try:
        return pwd.getpwnam(sandbox_user).pw_uid
    except KeyError:
        log.warning("Sandbox user '%s' not found, sandbox disabled", sandbox_user)
        return None


async def _exec_task(
    session: str, detail: str, timeout: int, sandbox_uid: int | None = None,
) -> tuple[str, str, bool]:
    """Run a shell command. Returns (stdout, stderr, success)."""
    denial = check_command_deny_list(detail)
    if denial:
        return "", denial, False

    workspace = _session_workspace(session)
    clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    try:
        kwargs: dict = dict(
            cwd=str(workspace),
            env=clean_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if sandbox_uid is not None:
            kwargs["user"] = sandbox_uid
        proc = await asyncio.create_subprocess_shell(detail, **kwargs)
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        return "", "Timed out", False

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    success = proc.returncode == 0
    return stdout, stderr, success


async def _skill_task(
    session: str,
    skill: dict,
    args: dict,
    plan_outputs: list[dict] | None,
    session_secrets: dict[str, str] | None,
    timeout: int,
    sandbox_uid: int | None = None,
) -> tuple[str, str, bool]:
    """Run a skill subprocess. Returns (stdout, stderr, success)."""
    workspace = _session_workspace(session)

    # Build input and env
    input_data = build_skill_input(
        skill, args, session, str(workspace),
        session_secrets=session_secrets,
        plan_outputs=plan_outputs,
    )
    env = build_skill_env(skill)

    # Find the python executable in the skill's venv
    skill_path = Path(skill["path"])
    venv_python = skill_path / ".venv" / "bin" / "python"
    if not venv_python.exists():
        # Fall back to system python if no venv
        venv_python = Path("python3")

    run_py = skill_path / "run.py"

    try:
        skill_kwargs: dict = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env=env,
        )
        if sandbox_uid is not None:
            skill_kwargs["user"] = sandbox_uid
        proc = await asyncio.create_subprocess_exec(
            str(venv_python), str(run_py), **skill_kwargs,
        )
        input_bytes = json.dumps(input_data).encode()
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=input_bytes), timeout=timeout,
        )
    except asyncio.TimeoutError:
        return "", "Timed out", False
    except OSError as e:
        return "", f"Skill executable not found: {e}", False

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    success = proc.returncode == 0
    return stdout, stderr, success


def _format_plan_outputs_for_msg(plan_outputs: list[dict]) -> str:
    """Format plan_outputs as readable text for the worker LLM prompt."""
    if not plan_outputs:
        return ""
    parts: list[str] = []
    for entry in plan_outputs:
        header = f"[{entry['index']}] {entry['type']}: {entry['detail']}"
        output = entry.get("output") or "(no output)"
        status = entry["status"]
        parts.append(f"{header}\nStatus: {status}\n{fence_content(output, 'TASK_OUTPUT')}")
    return "\n\n".join(parts)


async def _msg_task(
    config: Config,
    db: aiosqlite.Connection,
    session: str,
    detail: str,
    plan_outputs: list[dict] | None = None,
) -> str:
    """Generate a message via worker LLM. Returns the generated text."""
    system_prompt = _load_worker_prompt()

    # Build worker context: facts + summary + preceding outputs + detail
    sess = await get_session(db, session)
    summary = sess["summary"] if sess else ""
    facts = await get_facts(db)

    context_parts: list[str] = []
    if summary:
        context_parts.append(f"## Session Summary\n{summary}")
    if facts:
        facts_text = "\n".join(f"- {f['content']}" for f in facts)
        context_parts.append(f"## Known Facts\n{facts_text}")
    if plan_outputs:
        formatted = _format_plan_outputs_for_msg(plan_outputs)
        context_parts.append(f"## Preceding Task Outputs\n{formatted}")
    context_parts.append(f"## Task\n{detail}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(context_parts)},
    ]

    return await call_llm(config, "worker", messages)


async def _persist_plan_tasks(
    db: aiosqlite.Connection,
    plan_id: int,
    session: str,
    tasks: list[dict],
) -> list[int]:
    """Persist a list of task dicts to the DB. Returns list of task ids."""
    task_ids: list[int] = []
    for t in tasks:
        tid = await create_task(
            db, plan_id, session,
            type=t["type"], detail=t["detail"],
            skill=t.get("skill"), args=t.get("args"), expect=t.get("expect"),
        )
        task_ids.append(tid)
    return task_ids


async def _review_task(
    config: Config,
    db: aiosqlite.Connection,
    session: str,
    goal: str,
    task_row: dict,
    user_message: str,
) -> dict:
    """Review an exec/skill task. Returns review dict. Stores learning if present."""
    output = task_row.get("output") or ""
    stderr = task_row.get("stderr") or ""
    full_output = output
    if stderr:
        full_output += f"\n--- stderr ---\n{stderr}"

    review = await run_reviewer(
        config,
        goal=goal,
        detail=task_row["detail"],
        expect=task_row["expect"] or "",
        output=full_output,
        user_message=user_message,
    )

    # Store learning if present
    if review.get("learn"):
        await save_learning(db, review["learn"], session)
        log.info("Learning saved: %s", review["learn"][:100])

    return review


async def _execute_plan(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int,
    goal: str,
    user_message: str,
    exec_timeout: int,
    session_secrets: dict[str, str] | None = None,
    username: str | None = None,
) -> tuple[bool, str | None, list[dict], list[dict]]:
    """Execute a plan's tasks. Returns (success, replan_reason, completed, remaining).

    - success: True if all tasks completed successfully
    - replan_reason: reviewer reason if replan needed, None otherwise
    - completed: list of completed task dicts (with outputs)
    - remaining: list of unexecuted task dicts
    """
    tasks = await get_tasks_for_plan(db, plan_id)
    completed: list[dict] = []
    plan_outputs: list[dict] = []
    deploy_secrets = collect_deploy_secrets(config)

    for i, task_row in enumerate(tasks):
        task_id = task_row["id"]
        task_type = task_row["type"]
        detail = task_row["detail"]

        # --- Permission re-validation ---
        try:
            fresh_config = reload_config()
        except ConfigError as e:
            log.warning("Config reload failed: %s — using cached config", e)
            fresh_config = config

        perm = revalidate_permissions(
            fresh_config, username, task_type,
            skill_name=task_row.get("skill"),
        )
        if not perm.allowed:
            await update_task(db, task_id, "failed", output=perm.reason)
            remaining = [dict(t) for t in tasks[i + 1:]]
            _cleanup_plan_outputs(session)
            return False, None, completed, remaining

        sandbox_uid = _resolve_sandbox_uid(fresh_config) if perm.role == "user" else None

        await update_task(db, task_id, "running")

        if task_type == "exec":
            # Write plan_outputs.json before execution
            _write_plan_outputs(session, plan_outputs)

            stdout, stderr, success = await _exec_task(
                session, detail, exec_timeout, sandbox_uid=sandbox_uid,
            )
            stdout = sanitize_output(stdout, deploy_secrets, session_secrets or {})
            stderr = sanitize_output(stderr, deploy_secrets, session_secrets or {})
            status = "done" if success else "failed"
            await update_task(db, task_id, status, output=stdout, stderr=stderr)

            # Refresh task_row with output
            task_row = {**task_row, "output": stdout, "stderr": stderr, "status": status}

            # Accumulate plan output
            plan_outputs.append({
                "index": i + 1,
                "type": "exec",
                "detail": detail,
                "output": stdout,
                "status": status,
            })

            # Review exec tasks
            try:
                review = await _review_task(
                    config, db, session, goal, task_row, user_message,
                )
            except ReviewError as e:
                log.error("Review failed for task %d: %s", task_id, e)
                await update_task(db, task_id, "failed")
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

            if review["status"] == "replan":
                replan_reason = review["reason"]
                log.info("Reviewer requests replan: %s", replan_reason)
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, replan_reason, completed, remaining

            completed.append(task_row)

        elif task_type == "msg":
            try:
                text = await _msg_task(
                    config, db, session, detail,
                    plan_outputs=plan_outputs,
                )
                await update_task(db, task_id, "done", output=text)
                task_row = {**task_row, "output": text, "status": "done"}

                # Accumulate plan output
                plan_outputs.append({
                    "index": i + 1,
                    "type": "msg",
                    "detail": detail,
                    "output": text,
                    "status": "done",
                })

                # Webhook delivery
                sess = await get_session(db, session)
                webhook_url = sess.get("webhook") if sess else None
                if webhook_url:
                    is_final = i == len(tasks) - 1
                    await deliver_webhook(
                        webhook_url, session, task_id, text, is_final,
                        secret=str(config.settings.get("webhook_secret", "")),
                        max_payload=int(config.settings.get("webhook_max_payload", 0)),
                    )

                completed.append(task_row)
            except LLMError as e:
                log.error("Msg task %d LLM error: %s", task_id, e)
                await update_task(db, task_id, "failed", output=str(e))
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

        elif task_type == "skill":
            skill_name = task_row.get("skill")
            args_raw = task_row.get("args") or "{}"

            # Look up the skill
            installed = discover_skills()
            skill_info = next((s for s in installed if s["name"] == skill_name), None)

            if skill_info is None:
                error_msg = f"Skill '{skill_name}' not installed"
                await update_task(db, task_id, "failed", output=error_msg)
                task_row = {**task_row, "output": error_msg, "status": "failed"}
            else:
                # Parse and validate args
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError as e:
                    error_msg = f"Invalid skill args JSON: {e}"
                    await update_task(db, task_id, "failed", output=error_msg)
                    task_row = {**task_row, "output": error_msg, "status": "failed"}
                    args = None
                else:
                    validation_errors = validate_skill_args(args, skill_info["args_schema"])
                    if validation_errors:
                        error_msg = "Skill args validation failed: " + "; ".join(validation_errors)
                        await update_task(db, task_id, "failed", output=error_msg)
                        task_row = {**task_row, "output": error_msg, "status": "failed"}
                    else:
                        # Write plan_outputs.json before skill execution
                        _write_plan_outputs(session, plan_outputs)

                        stdout, stderr, success = await _skill_task(
                            session, skill_info, args, plan_outputs,
                            session_secrets, exec_timeout,
                            sandbox_uid=sandbox_uid,
                        )
                        stdout = sanitize_output(stdout, deploy_secrets, session_secrets or {})
                        stderr = sanitize_output(stderr, deploy_secrets, session_secrets or {})
                        status = "done" if success else "failed"
                        await update_task(db, task_id, status, output=stdout, stderr=stderr)
                        task_row = {**task_row, "output": stdout, "stderr": stderr, "status": status}

            # Accumulate plan output
            plan_outputs.append({
                "index": i + 1,
                "type": "skill",
                "detail": detail,
                "output": task_row.get("output") or "",
                "status": task_row["status"],
            })

            # Review skill tasks
            try:
                review = await _review_task(
                    config, db, session, goal, task_row, user_message,
                )
            except ReviewError as e:
                log.error("Review failed for skill task %d: %s", task_id, e)
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

            if review["status"] == "replan":
                replan_reason = review["reason"]
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, replan_reason, completed, remaining

            if task_row["status"] == "failed":
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

            completed.append(task_row)

    _cleanup_plan_outputs(session)
    return True, None, completed, []


def _build_replan_context(
    completed: list[dict],
    remaining: list[dict],
    replan_reason: str,
    replan_history: list[dict],
) -> str:
    """Build extra context for replanning."""
    parts: list[str] = []

    if completed:
        items = []
        for t in completed:
            out = (t.get("output") or "")[:500]
            out_fenced = fence_content(out, "TASK_OUTPUT") if out else "(no output)"
            items.append(f"- [{t['type']}] {t['detail']}: {t['status']} →\n{out_fenced}")
        parts.append("## Completed Tasks\n" + "\n".join(items))

    if remaining:
        items = [f"- [{t['type']}] {t['detail']}" for t in remaining]
        parts.append("## Remaining Tasks (not executed)\n" + "\n".join(items))

    parts.append(f"## Failure Reason\n{replan_reason}")

    if replan_history:
        items = []
        for h in replan_history:
            tried = ", ".join(h.get("what_was_tried", [])) or "nothing"
            items.append(f"- Goal: {h['goal']}, Tried: {tried}, Failure: {h['failure']}")
        parts.append(
            "## Previous Replan Attempts (DO NOT repeat these approaches)\n"
            + "\n".join(items)
        )

    return "\n\n".join(parts)


async def _apply_curator_result(
    db: aiosqlite.Connection, session: str, result: dict
) -> None:
    """Apply curator evaluations: promote facts, create pending questions, discard."""
    for ev in result.get("evaluations", []):
        lid = ev["learning_id"]
        verdict = ev["verdict"]
        if verdict == "promote":
            await save_fact(db, ev["fact"], source="curator", session=session)
            await update_learning(db, lid, "promoted")
        elif verdict == "ask":
            await save_pending_item(db, ev["question"], scope=session, source="curator")
            await update_learning(db, lid, "promoted")
        elif verdict == "discard":
            await update_learning(db, lid, "discarded")


async def run_worker(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    queue: asyncio.Queue,
):
    """Worker loop for a session. Drains queue, plans, executes tasks."""
    idle_timeout = int(config.settings.get("worker_idle_timeout", 300))
    exec_timeout = int(config.settings.get("exec_timeout", 120))
    max_replan_depth = int(config.settings.get("max_replan_depth", 3))

    while True:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
        except asyncio.TimeoutError:
            log.info("Worker idle timeout for session=%s, shutting down", session)
            break

        msg_id: int = msg["id"]
        content: str = msg["content"]
        user_role: str = msg["user_role"]
        user_skills: str | list[str] | None = msg.get("user_skills")
        username: str | None = msg.get("username")

        await mark_message_processed(db, msg_id)

        # Paraphraser — fetch untrusted messages, paraphrase if any
        paraphrased_context: str | None = None
        untrusted = await get_untrusted_messages(db, session)
        if untrusted:
            try:
                paraphrased_context = await run_paraphraser(config, untrusted)
            except ParaphraserError as e:
                log.warning("Paraphraser failed: %s", e)
                paraphrased_context = None

        # Plan
        try:
            plan = await run_planner(
                db, config, session, user_role, content,
                user_skills=user_skills,
                paraphrased_context=paraphrased_context,
            )
        except PlanError as e:
            log.error("Planning failed session=%s msg=%d: %s", session, msg_id, e)
            continue

        # Extract ephemeral secrets from plan
        session_secrets: dict[str, str] = {}
        if plan.get("secrets"):
            for s in plan["secrets"]:
                session_secrets[s["key"]] = s["value"]

        plan_id = await create_plan(db, session, msg_id, plan["goal"])
        await _persist_plan_tasks(db, plan_id, session, plan["tasks"])
        log.info("Plan %d: goal=%r, %d tasks", plan_id, plan["goal"], len(plan["tasks"]))

        # Execute with replan loop
        replan_history: list[dict] = []
        current_plan_id = plan_id
        current_goal = plan["goal"]
        replan_depth = 0

        while True:
            success, replan_reason, completed, remaining = await _execute_plan(
                db, config, session, current_plan_id, current_goal,
                content, exec_timeout, session_secrets=session_secrets,
                username=username,
            )

            if success:
                await update_plan_status(db, current_plan_id, "done")
                log.info("Plan %d done", current_plan_id)
                break

            if replan_reason is None:
                # Failed without replan request (LLM error, review error, etc.)
                await update_plan_status(db, current_plan_id, "failed")
                log.info("Plan %d failed (no replan)", current_plan_id)
                break

            # Replan requested
            replan_depth += 1
            if replan_depth > max_replan_depth:
                await update_plan_status(db, current_plan_id, "failed")
                # Mark remaining tasks as failed
                remaining_tasks = await get_tasks_for_plan(db, current_plan_id)
                for t in remaining_tasks:
                    if t["status"] == "pending":
                        await update_task(db, t["id"], "failed",
                                          output="Max replan depth reached")
                log.warning("Max replan depth (%d) reached for session=%s",
                            max_replan_depth, session)
                # Save a message notifying the user
                await save_message(
                    db, session, None, "system",
                    f"Max replan depth ({max_replan_depth}) reached. "
                    f"Last failure: {replan_reason}",
                    trusted=True, processed=True,
                )
                break

            # Mark current plan as failed and remaining tasks
            await update_plan_status(db, current_plan_id, "failed")
            current_tasks = await get_tasks_for_plan(db, current_plan_id)
            for t in current_tasks:
                if t["status"] == "pending":
                    await update_task(db, t["id"], "failed",
                                      output="Superseded by replan")

            # Build replan history
            tried = [
                f"[{t['type']}] {t['detail']}" for t in completed
            ]
            replan_history.append({
                "goal": current_goal,
                "failure": replan_reason,
                "what_was_tried": tried,
            })

            # Notify user about replan
            await save_message(
                db, session, None, "system",
                f"Replanning (attempt {replan_depth}/{max_replan_depth}): "
                f"{replan_reason}",
                trusted=True, processed=True,
            )

            # Call planner with enriched context
            replan_context = _build_replan_context(
                completed, remaining, replan_reason, replan_history,
            )
            enriched_message = f"{content}\n\n{replan_context}"

            try:
                new_plan = await run_planner(
                    db, config, session, user_role, enriched_message,
                    user_skills=user_skills,
                )
            except PlanError as e:
                log.error("Replan failed: %s", e)
                await save_message(
                    db, session, None, "system",
                    f"Replan failed: {e}",
                    trusted=True, processed=True,
                )
                break

            # Create new plan with parent_id
            new_plan_id = await create_plan(
                db, session, msg_id, new_plan["goal"],
                parent_id=current_plan_id,
            )
            await _persist_plan_tasks(db, new_plan_id, session, new_plan["tasks"])
            log.info("Replan %d (parent=%d): goal=%r, %d tasks",
                     new_plan_id, current_plan_id,
                     new_plan["goal"], len(new_plan["tasks"]))

            current_plan_id = new_plan_id
            current_goal = new_plan["goal"]

        # --- Post-plan knowledge processing ---

        # 1. Curator — process pending learnings
        learnings = await get_pending_learnings(db)
        if learnings:
            try:
                curator_result = await run_curator(config, learnings)
                await _apply_curator_result(db, session, curator_result)
            except CuratorError as e:
                log.error("Curator failed: %s", e)

        # 2. Summarizer — compress when threshold reached
        msg_count = await count_messages(db, session)
        if msg_count >= int(config.settings.get("summarize_threshold", 30)):
            try:
                sess = await get_session(db, session)
                current_summary = sess["summary"] if sess else ""
                oldest = await get_oldest_messages(db, session, limit=msg_count)
                new_summary = await run_summarizer(config, current_summary, oldest)
                await update_summary(db, session, new_summary)
            except SummarizerError as e:
                log.error("Summarizer failed: %s", e)

        # 3. Fact consolidation
        max_facts = int(config.settings.get("knowledge_max_facts", 50))
        all_facts = await get_facts(db)
        if len(all_facts) > max_facts:
            try:
                consolidated = await run_fact_consolidation(config, all_facts)
                if consolidated:
                    await delete_facts(db, [f["id"] for f in all_facts])
                    for text in consolidated:
                        await save_fact(db, text, source="consolidation")
            except SummarizerError as e:
                log.error("Fact consolidation failed: %s", e)
