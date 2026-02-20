"""Per-session asyncio worker — drains queue, plans, executes tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import time
from pathlib import Path

import aiosqlite

from kiso import audit
from kiso.config import ConfigError, reload_config
from kiso.log import SessionLogger
from kiso.security import (
    check_command_deny_list,
    collect_deploy_secrets,
    fence_content,
    revalidate_permissions,
    sanitize_output,
)
from kiso.brain import (
    CuratorError,
    ExecTranslatorError,
    MessengerError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    run_curator,
    run_exec_translator,
    run_fact_consolidation,
    run_messenger,
    run_paraphraser,
    run_planner,
    run_reviewer,
    run_summarizer,
)
from kiso.config import Config, KISO_DIR
from kiso.llm import LLMBudgetExceeded, LLMError, call_llm, clear_llm_budget, get_usage_summary, reset_usage_tracking, set_llm_budget
from kiso.skills import (
    SkillError,
    build_skill_env,
    build_skill_input,
    discover_skills,
    validate_skill_args,
)
from kiso.sysenv import get_system_env, build_system_env_section
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
    update_plan_usage,
    update_summary,
    update_task,
    update_task_command,
    update_task_review,
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



def _session_workspace(session: str, sandbox_uid: int | None = None) -> Path:
    """Return and ensure the session workspace directory exists."""
    workspace = KISO_DIR / "sessions" / session
    workspace.mkdir(parents=True, exist_ok=True)
    if sandbox_uid is not None:
        os.chown(workspace, sandbox_uid, sandbox_uid)
        os.chmod(workspace, 0o700)
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


def _ensure_sandbox_user(session: str) -> int | None:
    """Create or reuse a per-session Linux user. Returns UID or None on failure."""
    import hashlib
    import subprocess

    # Deterministic username from session (max 32 chars for Linux)
    h = hashlib.sha256(session.encode()).hexdigest()[:12]
    username = f"kiso-s-{h}"
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError:
        pass
    # Create user — requires root
    try:
        subprocess.run(
            ["useradd", "--system", "--no-create-home",
             "--shell", "/usr/sbin/nologin", username],
            check=True, capture_output=True,
        )
        return pwd.getpwnam(username).pw_uid
    except (subprocess.CalledProcessError, KeyError, FileNotFoundError) as exc:
        log.warning("Cannot create sandbox user '%s': %s", username, exc)
        return None


def _truncate_output(text: str, limit: int) -> str:
    """Truncate text to *limit* characters, appending a marker if truncated."""
    if limit > 0 and len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


async def _exec_task(
    session: str, detail: str, timeout: int, sandbox_uid: int | None = None,
    max_output_size: int = 0,
) -> tuple[str, str, bool]:
    """Run a shell command. Returns (stdout, stderr, success).

    When *max_output_size* > 0, stdout and stderr are each truncated to
    that many characters to prevent memory exhaustion from oversized output.
    """
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

    stdout = _truncate_output(stdout_bytes.decode(errors="replace"), max_output_size)
    stderr = _truncate_output(stderr_bytes.decode(errors="replace"), max_output_size)
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
    max_output_size: int = 0,
) -> tuple[str, str, bool]:
    """Run a skill subprocess. Returns (stdout, stderr, success).

    When *max_output_size* > 0, stdout and stderr are each truncated to
    that many characters to prevent memory exhaustion from oversized output.
    """
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

    stdout = _truncate_output(stdout_bytes.decode(errors="replace"), max_output_size)
    stderr = _truncate_output(stderr_bytes.decode(errors="replace"), max_output_size)
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
    """Generate a user-facing message via the messenger brain role."""
    outputs_text = _format_plan_outputs_for_msg(plan_outputs) if plan_outputs else ""
    return await run_messenger(db, config, session, detail, outputs_text)


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

    success = task_row.get("status") == "done"
    review = await run_reviewer(
        config,
        goal=goal,
        detail=task_row["detail"],
        expect=task_row["expect"] or "",
        output=full_output,
        user_message=user_message,
        session=session,
        success=success,
    )

    # Store learning if present
    has_learning = bool(review.get("learn"))
    if has_learning:
        await save_learning(db, review["learn"], session)
        log.info("Learning saved: %s", review["learn"][:100])

    audit.log_review(session, task_row.get("id", 0), review["status"], has_learning)

    await update_task_review(
        db, task_row["id"], review["status"],
        reason=review.get("reason"), learning=review.get("learn"),
    )

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
    cancel_event: asyncio.Event | None = None,
    slog: SessionLogger | None = None,
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
    max_output_size = int(config.settings.get("max_output_size", 0))

    for i, task_row in enumerate(tasks):
        # --- Cancel check ---
        if cancel_event is not None and cancel_event.is_set():
            for t in tasks[i:]:
                await update_task(db, t["id"], "cancelled")
                audit.log_task(
                    session, t["id"], t["type"], t["detail"],
                    "cancelled", 0, 0,
                    deploy_secrets=deploy_secrets,
                    session_secrets=session_secrets or {},
                )
            _cleanup_plan_outputs(session)
            return False, "cancelled", completed, [dict(t) for t in tasks[i:]]

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
            audit.log_task(
                session, task_id, task_type, detail, "failed", 0, 0,
                deploy_secrets=deploy_secrets,
                session_secrets=session_secrets or {},
            )
            remaining = [dict(t) for t in tasks[i + 1:]]
            _cleanup_plan_outputs(session)
            return False, None, completed, remaining

        sandbox_uid = _ensure_sandbox_user(session) if perm.role == "user" else None
        if sandbox_uid is not None:
            _session_workspace(session, sandbox_uid=sandbox_uid)

        await update_task(db, task_id, "running")
        if slog:
            slog.info("Task %d started: [%s] %s", task_id, task_type, detail[:120])

        if task_type == "exec":
            # Write plan_outputs.json before execution
            _write_plan_outputs(session, plan_outputs)

            # Translate natural-language detail → shell command
            sys_env = get_system_env(config)
            sys_env_text = build_system_env_section(sys_env)
            outputs_text = _format_plan_outputs_for_msg(plan_outputs)
            try:
                command = await run_exec_translator(
                    config, detail, sys_env_text,
                    plan_outputs_text=outputs_text, session=session,
                )
            except ExecTranslatorError as e:
                log.error("Exec translation failed for task %d: %s", task_id, e)
                await update_task(db, task_id, "failed", output="", stderr=str(e))
                audit.log_task(
                    session, task_id, "exec", detail, "failed", 0, 0,
                    deploy_secrets=deploy_secrets,
                    session_secrets=session_secrets or {},
                )
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

            await update_task_command(db, task_id, command)

            if slog:
                slog.info("Task %d translated: %s → %s", task_id, detail[:80], command[:120])

            t0 = time.perf_counter()
            stdout, stderr, success = await _exec_task(
                session, command, exec_timeout, sandbox_uid=sandbox_uid,
                max_output_size=max_output_size,
            )
            task_duration_ms = int((time.perf_counter() - t0) * 1000)
            stdout = sanitize_output(stdout, deploy_secrets, session_secrets or {})
            stderr = sanitize_output(stderr, deploy_secrets, session_secrets or {})
            status = "done" if success else "failed"
            await update_task(db, task_id, status, output=stdout, stderr=stderr)

            audit.log_task(
                session, task_id, "exec", detail, status, task_duration_ms,
                len(stdout), deploy_secrets=deploy_secrets,
                session_secrets=session_secrets or {},
            )
            if slog:
                slog.info("Task %d done: [exec] %s (%dms)", task_id, status, task_duration_ms)

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
                if slog:
                    slog.info("Review → replan: %s", replan_reason)
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, replan_reason, completed, remaining

            if slog:
                slog.info("Review → %s", review["status"])
            completed.append(task_row)

        elif task_type == "msg":
            try:
                t0 = time.perf_counter()
                text = await _msg_task(
                    config, db, session, detail,
                    plan_outputs=plan_outputs,
                )
                task_duration_ms = int((time.perf_counter() - t0) * 1000)
                await update_task(db, task_id, "done", output=text)
                task_row = {**task_row, "output": text, "status": "done"}

                audit.log_task(
                    session, task_id, "msg", detail, "done", task_duration_ms,
                    len(text), deploy_secrets=deploy_secrets,
                    session_secrets=session_secrets or {},
                )
                if slog:
                    slog.info("Task %d done: [msg] done (%dms)", task_id, task_duration_ms)

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
                    wh_success, wh_status, wh_attempts = await deliver_webhook(
                        webhook_url, session, task_id, text, is_final,
                        secret=str(config.settings.get("webhook_secret", "")),
                        max_payload=int(config.settings.get("webhook_max_payload", 0)),
                    )
                    audit.log_webhook(
                        session, task_id, webhook_url, wh_status, wh_attempts,
                        deploy_secrets=deploy_secrets,
                        session_secrets=session_secrets or {},
                    )

                completed.append(task_row)
            except (LLMError, MessengerError) as e:
                task_duration_ms = int((time.perf_counter() - t0) * 1000)
                log.error("Msg task %d messenger error: %s", task_id, e)
                await update_task(db, task_id, "failed", output=str(e))
                audit.log_task(
                    session, task_id, "msg", detail, "failed",
                    task_duration_ms, 0,
                    deploy_secrets=deploy_secrets,
                    session_secrets=session_secrets or {},
                )
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

        elif task_type == "skill":
            skill_name = task_row.get("skill")
            args_raw = task_row.get("args") or "{}"

            # Look up the skill
            installed = discover_skills()
            skill_info = next((s for s in installed if s["name"] == skill_name), None)

            t0 = time.perf_counter()

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
                            max_output_size=max_output_size,
                        )
                        stdout = sanitize_output(stdout, deploy_secrets, session_secrets or {})
                        stderr = sanitize_output(stderr, deploy_secrets, session_secrets or {})
                        status = "done" if success else "failed"
                        await update_task(db, task_id, status, output=stdout, stderr=stderr)
                        task_row = {**task_row, "output": stdout, "stderr": stderr, "status": status}

            task_duration_ms = int((time.perf_counter() - t0) * 1000)
            audit.log_task(
                session, task_id, "skill", detail, task_row["status"],
                task_duration_ms, len(task_row.get("output") or ""),
                deploy_secrets=deploy_secrets,
                session_secrets=session_secrets or {},
            )
            if slog:
                slog.info("Task %d done: [skill] %s (%dms)", task_id, task_row["status"], task_duration_ms)

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
                if slog:
                    slog.info("Review → replan: %s", replan_reason)
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, replan_reason, completed, remaining

            if slog:
                slog.info("Review → %s", review["status"])

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


def _build_cancel_summary(
    completed: list[dict], remaining: list[dict], goal: str,
) -> str:
    """Build a detail string for the worker LLM summarising a cancel."""
    parts: list[str] = [f"The user cancelled the plan: {goal}"]

    if completed:
        items = [f"- [{t['type']}] {t['detail']}" for t in completed]
        parts.append(f"Completed ({len(completed)}):\n" + "\n".join(items))
    else:
        parts.append("No tasks were completed.")

    if remaining:
        items = [f"- [{t['type']}] {t['detail']}" for t in remaining]
        parts.append(f"Skipped ({len(remaining)}):\n" + "\n".join(items))

    parts.append(
        "Generate a brief message: what was done, what wasn't, "
        "and suggest next steps."
    )
    return "\n\n".join(parts)


def _build_failure_summary(
    completed: list[dict], remaining: list[dict], goal: str,
    reason: str | None = None,
) -> str:
    """Build a detail string for the messenger LLM summarising a plan failure."""
    parts: list[str] = [f"The plan failed: {goal}"]

    if reason:
        parts.append(f"Failure reason: {reason}")

    if completed:
        items = [f"- [{t['type']}] {t['detail']}" for t in completed]
        parts.append(f"Completed ({len(completed)}):\n" + "\n".join(items))
    else:
        parts.append("No tasks were completed.")

    if remaining:
        items = [f"- [{t['type']}] {t['detail']}" for t in remaining]
        parts.append(f"Failed/Skipped ({len(remaining)}):\n" + "\n".join(items))

    parts.append(
        "Generate a brief message explaining what went wrong "
        "and suggest next steps."
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
    cancel_event: asyncio.Event | None = None,
):
    """Worker loop for a session. Drains queue, plans, executes tasks."""
    idle_timeout = int(config.settings.get("worker_idle_timeout", 300))
    exec_timeout = int(config.settings.get("exec_timeout", 120))
    max_replan_depth = int(config.settings.get("max_replan_depth", 3))
    slog = SessionLogger(session)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                log.info("Worker idle timeout for session=%s, shutting down", session)
                slog.info("Worker idle — shutting down")
                break

            try:
                await _process_message(db, config, session, msg, queue, cancel_event,
                                       idle_timeout, exec_timeout, max_replan_depth,
                                       slog=slog)
            except Exception:
                log.exception("Unexpected error processing message in session=%s", session)
                slog.error("Unexpected error processing message")
                continue
    finally:
        slog.close()


async def _process_message(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    msg: dict,
    queue: asyncio.Queue,
    cancel_event: asyncio.Event | None,
    idle_timeout: int,
    exec_timeout: int,
    max_replan_depth: int,
    slog: SessionLogger | None = None,
):
    """Process a single message. Extracted for crash recovery wrapping."""
    msg_id: int = msg["id"]
    content: str = msg["content"]
    user_role: str = msg["user_role"]
    user_skills: str | list[str] | None = msg.get("user_skills")
    username: str | None = msg.get("username")

    if slog:
        slog.info("Message received: user=%s, %d chars", username or "?", len(content))

    # Per-message LLM call budget and usage tracking
    max_llm_calls = int(config.settings.get("max_llm_calls_per_message", 200))
    set_llm_budget(max_llm_calls)
    reset_usage_tracking()

    await mark_message_processed(db, msg_id)

    # Paraphraser — fetch untrusted messages, paraphrase if any
    paraphrased_context: str | None = None
    untrusted = await get_untrusted_messages(db, session)
    if untrusted:
        try:
            paraphrased_context = await run_paraphraser(config, untrusted, session=session)
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
        error_text = f"Planning failed: {e}"
        # Create a failed plan + msg task so the CLI can detect the failure
        fail_plan_id = await create_plan(db, session, msg_id, "Failed")
        fail_task_id = await create_task(db, fail_plan_id, session, "msg", error_text)
        await update_task(db, fail_task_id, status="done", output=error_text)
        await update_plan_status(db, fail_plan_id, "failed")
        await save_message(db, session, None, "system", error_text, trusted=True, processed=True)
        sess = await get_session(db, session)
        webhook_url = sess.get("webhook") if sess else None
        if webhook_url:
            await deliver_webhook(webhook_url, session, 0, error_text, True,
                                  secret=str(config.settings.get("webhook_secret", "")),
                                  max_payload=int(config.settings.get("webhook_max_payload", 0)))
        return

    # Extract ephemeral secrets from plan
    session_secrets: dict[str, str] = {}
    if plan.get("secrets"):
        for s in plan["secrets"]:
            try:
                session_secrets[s["key"]] = s["value"]
            except (KeyError, TypeError) as e:
                log.warning("Malformed secret entry (skipped): %s", e)
        if session_secrets:
            log.info("%d secrets extracted", len(session_secrets))

    # Sanitize secrets from task detail/args before DB storage
    deploy_secrets = collect_deploy_secrets(config)
    for t in plan["tasks"]:
        t["detail"] = sanitize_output(t["detail"], deploy_secrets, session_secrets)
        if t.get("args"):
            t["args"] = sanitize_output(t["args"], deploy_secrets, session_secrets)

    plan_id = await create_plan(db, session, msg_id, plan["goal"])
    await _persist_plan_tasks(db, plan_id, session, plan["tasks"])
    log.info("Plan %d: goal=%r, %d tasks", plan_id, plan["goal"], len(plan["tasks"]))
    if slog:
        slog.info("Plan %d created: %s (%d tasks)", plan_id, plan["goal"], len(plan["tasks"]))

    # Execute with replan loop
    replan_history: list[dict] = []
    current_plan_id = plan_id
    current_goal = plan["goal"]
    replan_depth = 0

    while True:
        success, replan_reason, completed, remaining = await _execute_plan(
            db, config, session, current_plan_id, current_goal,
            content, exec_timeout, session_secrets=session_secrets,
            username=username, cancel_event=cancel_event, slog=slog,
        )

        if success:
            await update_plan_status(db, current_plan_id, "done")
            log.info("Plan %d done", current_plan_id)
            if slog:
                slog.info("Plan %d done", current_plan_id)
            break

        # --- Cancel handling ---
        if replan_reason == "cancelled":
            await update_plan_status(db, current_plan_id, "cancelled")
            cancel_detail = _build_cancel_summary(
                completed, remaining, current_goal,
            )
            try:
                cancel_text = await _msg_task(
                    config, db, session, cancel_detail,
                )
            except (LLMError, MessengerError):
                cancel_text = cancel_detail  # fallback to raw summary
            cancel_task_id = await create_task(
                db, current_plan_id, session, "msg", cancel_detail,
            )
            await update_task(
                db, cancel_task_id, status="done", output=cancel_text,
            )
            await save_message(
                db, session, None, "system", cancel_text,
                trusted=True, processed=True,
            )
            # Webhook
            sess = await get_session(db, session)
            webhook_url = sess.get("webhook") if sess else None
            if webhook_url:
                wh_success, wh_status, wh_attempts = await deliver_webhook(
                    webhook_url, session, 0, cancel_text, True,
                    secret=str(config.settings.get("webhook_secret", "")),
                    max_payload=int(
                        config.settings.get("webhook_max_payload", 0)
                    ),
                )
                audit.log_webhook(
                    session, 0, webhook_url, wh_status, wh_attempts,
                    deploy_secrets=collect_deploy_secrets(config),
                    session_secrets=session_secrets or {},
                )
            if cancel_event is not None:
                cancel_event.clear()  # reset for next message
            break

        if replan_reason is None:
            # Failed without replan request (LLM error, review error, etc.)
            await update_plan_status(db, current_plan_id, "failed")
            log.info("Plan %d failed (no replan)", current_plan_id)
            if slog:
                slog.info("Plan %d failed", current_plan_id)
            # Recovery msg task so the user gets LLM-generated feedback
            fail_detail = _build_failure_summary(
                completed, remaining, current_goal,
            )
            try:
                fail_text = await _msg_task(config, db, session, fail_detail)
            except (LLMError, MessengerError):
                fail_text = fail_detail
            fail_task_id = await create_task(
                db, current_plan_id, session, "msg", fail_detail,
            )
            await update_task(db, fail_task_id, status="done", output=fail_text)
            await save_message(
                db, session, None, "system", fail_text,
                trusted=True, processed=True,
            )
            sess = await get_session(db, session)
            webhook_url = sess.get("webhook") if sess else None
            if webhook_url:
                await deliver_webhook(
                    webhook_url, session, 0, fail_text, True,
                    secret=str(config.settings.get("webhook_secret", "")),
                    max_payload=int(
                        config.settings.get("webhook_max_payload", 0)
                    ),
                )
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
            # Recovery msg task so the user gets LLM-generated feedback
            replan_detail = _build_failure_summary(
                completed, remaining, current_goal,
                reason=f"Max replan depth ({max_replan_depth}) reached. "
                       f"Last failure: {replan_reason}",
            )
            try:
                replan_text = await _msg_task(config, db, session, replan_detail)
            except (LLMError, MessengerError):
                replan_text = replan_detail
            replan_task_id = await create_task(
                db, current_plan_id, session, "msg", replan_detail,
            )
            await update_task(
                db, replan_task_id, status="done", output=replan_text,
            )
            await save_message(
                db, session, None, "system", replan_text,
                trusted=True, processed=True,
            )
            sess = await get_session(db, session)
            webhook_url = sess.get("webhook") if sess else None
            if webhook_url:
                await deliver_webhook(
                    webhook_url, session, 0, replan_text, True,
                    secret=str(config.settings.get("webhook_secret", "")),
                    max_payload=int(
                        config.settings.get("webhook_max_payload", 0)
                    ),
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

        # --- Cancel check before replan ---
        if cancel_event is not None and cancel_event.is_set():
            await update_plan_status(db, current_plan_id, "cancelled")
            break

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
        if slog:
            slog.info("Replan %d: %s (%d tasks, attempt %d/%d)",
                       new_plan_id, new_plan["goal"], len(new_plan["tasks"]),
                       replan_depth, max_replan_depth)

        current_plan_id = new_plan_id
        current_goal = new_plan["goal"]

    # --- Invalidate system env cache (exec tasks may have changed the system) ---
    from kiso.sysenv import invalidate_cache
    invalidate_cache()

    # --- Post-plan knowledge processing ---

    # 1. Curator — process pending learnings
    learnings = await get_pending_learnings(db)
    if learnings:
        try:
            curator_result = await asyncio.wait_for(
                run_curator(config, learnings, session=session),
                timeout=exec_timeout,
            )
            await _apply_curator_result(db, session, curator_result)
        except asyncio.TimeoutError:
            log.warning("Curator timed out after %ds", exec_timeout)
        except CuratorError as e:
            log.error("Curator failed: %s", e)

    # 2. Summarizer — compress when threshold reached
    msg_count = await count_messages(db, session)
    if msg_count >= int(config.settings.get("summarize_threshold", 30)):
        try:
            sess = await get_session(db, session)
            current_summary = sess["summary"] if sess else ""
            oldest = await get_oldest_messages(db, session, limit=msg_count)
            new_summary = await asyncio.wait_for(
                run_summarizer(config, current_summary, oldest, session=session),
                timeout=exec_timeout,
            )
            await update_summary(db, session, new_summary)
        except asyncio.TimeoutError:
            log.warning("Summarizer timed out after %ds", exec_timeout)
        except SummarizerError as e:
            log.error("Summarizer failed: %s", e)

    # 3. Fact consolidation
    max_facts = int(config.settings.get("knowledge_max_facts", 50))
    all_facts = await get_facts(db)
    if len(all_facts) > max_facts:
        try:
            consolidated = await asyncio.wait_for(
                run_fact_consolidation(config, all_facts, session=session),
                timeout=exec_timeout,
            )
            if consolidated:
                if len(consolidated) < len(all_facts) * 0.3:
                    log.warning("Fact consolidation shrank %d → %d (< 30%%), skipping",
                                len(all_facts), len(consolidated))
                else:
                    consolidated = [f for f in consolidated if isinstance(f, str) and len(f.strip()) >= 10]
                    if consolidated:
                        await delete_facts(db, [f["id"] for f in all_facts])
                        for text in consolidated:
                            await save_fact(db, text, source="consolidation")
                    else:
                        log.warning("All consolidated facts filtered out, preserving originals")
        except asyncio.TimeoutError:
            log.warning("Fact consolidation timed out after %ds", exec_timeout)
        except SummarizerError as e:
            log.error("Fact consolidation failed: %s", e)

    # --- Store token usage on the plan ---
    usage = get_usage_summary()
    if current_plan_id and (usage["input_tokens"] or usage["output_tokens"]):
        await update_plan_usage(
            db, current_plan_id,
            usage["input_tokens"], usage["output_tokens"], usage["model"],
        )

    clear_llm_budget()
