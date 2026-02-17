"""Per-session asyncio worker — drains queue, plans, executes tasks."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiosqlite

from kiso.brain import PlanError, ReviewError, run_planner, run_reviewer
from kiso.config import Config, KISO_DIR
from kiso.llm import LLMError, call_llm
from kiso.store import (
    create_plan,
    create_task,
    get_facts,
    get_session,
    get_tasks_for_plan,
    mark_message_processed,
    save_learning,
    save_message,
    update_plan_status,
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


async def _exec_task(
    session: str, detail: str, timeout: int
) -> tuple[str, str, bool]:
    """Run a shell command. Returns (stdout, stderr, success)."""
    workspace = _session_workspace(session)
    clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    try:
        proc = await asyncio.create_subprocess_shell(
            detail,
            cwd=str(workspace),
            env=clean_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        return "", "Timed out", False

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    success = proc.returncode == 0
    return stdout, stderr, success


async def _msg_task(
    config: Config, db: aiosqlite.Connection, session: str, detail: str
) -> str:
    """Generate a message via worker LLM. Returns the generated text."""
    system_prompt = _load_worker_prompt()

    # Build worker context: facts + summary + detail
    sess = await get_session(db, session)
    summary = sess["summary"] if sess else ""
    facts = await get_facts(db)

    context_parts: list[str] = []
    if summary:
        context_parts.append(f"## Session Summary\n{summary}")
    if facts:
        facts_text = "\n".join(f"- {f['content']}" for f in facts)
        context_parts.append(f"## Known Facts\n{facts_text}")
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
) -> tuple[bool, str | None, list[dict], list[dict]]:
    """Execute a plan's tasks. Returns (success, replan_reason, completed, remaining).

    - success: True if all tasks completed successfully
    - replan_reason: reviewer reason if replan needed, None otherwise
    - completed: list of completed task dicts (with outputs)
    - remaining: list of unexecuted task dicts
    """
    tasks = await get_tasks_for_plan(db, plan_id)
    completed: list[dict] = []
    replan_reason: str | None = None

    for i, task_row in enumerate(tasks):
        task_id = task_row["id"]
        task_type = task_row["type"]
        detail = task_row["detail"]

        await update_task(db, task_id, "running")

        if task_type == "exec":
            stdout, stderr, success = await _exec_task(session, detail, exec_timeout)
            status = "done" if success else "failed"
            await update_task(db, task_id, status, output=stdout, stderr=stderr)

            # Refresh task_row with output
            task_row = {**task_row, "output": stdout, "stderr": stderr, "status": status}

            # Review exec tasks
            try:
                review = await _review_task(
                    config, db, session, goal, task_row, user_message,
                )
            except ReviewError as e:
                log.error("Review failed for task %d: %s", task_id, e)
                # Treat review failure as plan failure
                await update_task(db, task_id, "failed")
                remaining = [dict(t) for t in tasks[i + 1:]]
                return False, None, completed, remaining

            if review["status"] == "replan":
                replan_reason = review["reason"]
                log.info("Reviewer requests replan: %s", replan_reason)
                remaining = [dict(t) for t in tasks[i + 1:]]
                return False, replan_reason, completed, remaining

            completed.append(task_row)

        elif task_type == "msg":
            try:
                text = await _msg_task(config, db, session, detail)
                await update_task(db, task_id, "done", output=text)
                task_row = {**task_row, "output": text, "status": "done"}
                completed.append(task_row)
            except LLMError as e:
                log.error("Msg task %d LLM error: %s", task_id, e)
                await update_task(db, task_id, "failed", output=str(e))
                remaining = [dict(t) for t in tasks[i + 1:]]
                return False, None, completed, remaining

        elif task_type == "skill":
            # Skills not implemented until M7
            await update_task(db, task_id, "failed", output="Skills not yet implemented")
            # Review skill tasks too
            task_row = {**task_row, "output": "Skills not yet implemented", "status": "failed"}
            try:
                review = await _review_task(
                    config, db, session, goal, task_row, user_message,
                )
            except ReviewError as e:
                log.error("Review failed for skill task %d: %s", task_id, e)
                remaining = [dict(t) for t in tasks[i + 1:]]
                return False, None, completed, remaining

            if review["status"] == "replan":
                replan_reason = review["reason"]
                remaining = [dict(t) for t in tasks[i + 1:]]
                return False, replan_reason, completed, remaining

            # Even if reviewer says ok (unlikely), skill still failed
            remaining = [dict(t) for t in tasks[i + 1:]]
            return False, None, completed, remaining

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
            items.append(f"- [{t['type']}] {t['detail']}: {t['status']} → {out}")
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

        await mark_message_processed(db, msg_id)

        # Plan
        try:
            plan = await run_planner(db, config, session, user_role, content)
        except PlanError as e:
            log.error("Planning failed session=%s msg=%d: %s", session, msg_id, e)
            continue

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
                content, exec_timeout,
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
                )
            except PlanError as e:
                log.error("Replan failed: %s", e)
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
