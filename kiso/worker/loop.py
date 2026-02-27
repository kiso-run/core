"""Session loop, plan orchestration, and message processing for the kiso worker."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiosqlite

from kiso import audit
from kiso.config import ConfigError, KISO_DIR, reload_config
from kiso.log import SessionLogger
from kiso.security import (
    collect_deploy_secrets,
    revalidate_permissions,
    sanitize_output,
)
from kiso.brain import (
    ClassifierError,
    CuratorError,
    ExecTranslatorError,
    MessengerError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    classify_message,
    run_curator,
    run_exec_translator,
    run_fact_consolidation,
    run_messenger,
    run_paraphraser,
    run_planner,
    run_reviewer,
    run_summarizer,
)
from kiso.config import Config, setting_bool
from kiso.llm import LLMBudgetExceeded, LLMError, clear_llm_budget, get_usage_index, get_usage_since, get_usage_summary, reset_usage_tracking, set_llm_budget
from kiso.skills import (
    SkillError,
    discover_skills,
    validate_skill_args,
)
from kiso.sysenv import get_system_env, build_system_env_section
from kiso.webhook import deliver_webhook
from kiso.worker.search import SearcherError, _search_task
from kiso.store import (
    append_task_llm_call,
    archive_low_confidence_facts,
    count_messages,
    create_plan,
    create_task,
    decay_facts,
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
    update_fact_usage,
    update_learning,
    update_plan_status,
    update_plan_usage,
    update_summary,
    update_task,
    update_task_command,
    update_task_review,
    update_task_retry_count,
    update_task_substatus,
    update_task_usage,
)

from kiso.worker.utils import (
    _build_cancel_summary,
    _build_failure_summary,
    _build_replan_context,
    _cleanup_plan_outputs,
    _ensure_sandbox_user,
    _format_plan_outputs_for_msg,
    _report_pub_files,
    _session_workspace,
    _write_plan_outputs,
)
from kiso.worker.exec import _exec_task
from kiso.worker.skill import _skill_task

log = logging.getLogger(__name__)


async def _append_calls(
    db: aiosqlite.Connection, task_id: int, idx_before: int
) -> None:
    """Append individual LLM call entries (since idx_before) to the task row.

    Called right after each LLM step (translator, reviewer, messenger,
    searcher) so verbose panels appear incrementally in the CLI instead of
    only when the task finishes.

    Failures are caught and logged so a transient usage-tracking error never
    crashes the worker or corrupts the task result.
    """
    try:
        usage = get_usage_since(idx_before)
        for call in usage.get("calls") or []:
            await append_task_llm_call(db, task_id, call)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_append_calls: failed to store LLM calls for task %d: %s", task_id, exc
        )


async def _deliver_webhook_if_configured(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    task_id: int,
    content: str,
    final: bool,
    deploy_secrets: dict[str, str] | None = None,
    session_secrets: dict[str, str] | None = None,
) -> None:
    """Deliver a webhook if the session has one configured. No-op otherwise."""
    sess = await get_session(db, session)
    webhook_url = sess.get("webhook") if sess else None
    if not webhook_url:
        return
    wh_success, wh_status, wh_attempts = await deliver_webhook(
        webhook_url, session, task_id, content, final,
        secret=str(config.settings["webhook_secret"]),
        max_payload=int(config.settings["webhook_max_payload"]),
    )
    audit.log_webhook(
        session, task_id, webhook_url, wh_status, wh_attempts,
        deploy_secrets=deploy_secrets or {},
        session_secrets=session_secrets or {},
    )


async def _msg_task(
    config: Config,
    db: aiosqlite.Connection,
    session: str,
    detail: str,
    plan_outputs: list[dict] | None = None,
    goal: str = "",
) -> str:
    """Generate a user-facing message via the messenger brain role."""
    outputs_text = _format_plan_outputs_for_msg(plan_outputs) if plan_outputs else ""
    return await run_messenger(db, config, session, detail, outputs_text, goal=goal)


async def _post_plan_knowledge(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int | None,
    exec_timeout: int,
) -> None:
    """Run post-plan knowledge processing: curator, summarizer, fact consolidation.

    Called after both normal and fast-path message processing to keep
    the knowledge base current regardless of which path was taken.
    """
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
    if msg_count >= int(config.settings["summarize_threshold"]):
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

    # 3. Fact consolidation — uses full (admin-level) view so the count check
    # reflects the true global fact table, not just the current session's view.
    max_facts = int(config.settings["knowledge_max_facts"])
    all_facts = await get_facts(db, is_admin=True)
    if len(all_facts) > max_facts:
        try:
            consolidated = await asyncio.wait_for(
                run_fact_consolidation(config, all_facts, session=session),
                timeout=exec_timeout,
            )
            if consolidated:
                min_ratio = float(config.settings["fact_consolidation_min_ratio"])
                if len(consolidated) < len(all_facts) * min_ratio:
                    log.warning("Fact consolidation shrank %d → %d (< %.0f%%), skipping",
                                len(all_facts), len(consolidated), min_ratio * 100)
                else:
                    consolidated = [
                        f for f in consolidated
                        if isinstance(f, dict) and isinstance(f.get("content"), str)
                        and len(f["content"].strip()) >= 3
                    ]
                    if consolidated:
                        await delete_facts(db, [f["id"] for f in all_facts])
                        for fact in consolidated:
                            category = fact.get("category", "general")
                            await save_fact(
                                db, fact["content"], source="consolidation",
                                category=category,
                                confidence=fact.get("confidence", 1.0),
                                # Preserve session scoping: user facts belong to the
                                # session that triggered consolidation; all other
                                # categories are global (session=None).
                                session=session if category == "user" else None,
                            )
                    else:
                        log.warning("All consolidated facts filtered out, preserving originals")
        except asyncio.TimeoutError:
            log.warning("Fact consolidation timed out after %ds", exec_timeout)
        except SummarizerError as e:
            log.error("Fact consolidation failed: %s", e)

    # 4. Decay stale facts
    decay_days = int(config.settings["fact_decay_days"])
    decay_rate = float(config.settings["fact_decay_rate"])
    try:
        decayed = await decay_facts(db, decay_days=decay_days, decay_rate=decay_rate)
        if decayed:
            log.info("Decayed %d stale facts", decayed)
    except Exception as e:
        log.error("Fact decay failed: %s", e)

    # 5. Archive low-confidence facts
    archive_threshold = float(config.settings["fact_archive_threshold"])
    try:
        archived = await archive_low_confidence_facts(db, threshold=archive_threshold)
        if archived:
            log.info("Archived %d low-confidence facts", archived)
    except Exception as e:
        log.error("Fact archiving failed: %s", e)

    # Update token usage with post-plan processing tokens
    final_usage = get_usage_summary()
    if plan_id and (final_usage["input_tokens"] or final_usage["output_tokens"]):
        await update_plan_usage(
            db, plan_id,
            final_usage["input_tokens"], final_usage["output_tokens"],
            final_usage["model"],
        )


async def _fast_path_chat(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    msg_id: int,
    content: str,
    slog: SessionLogger | None = None,
) -> int:
    """Fast path for chat messages: skip planner, go straight to messenger.

    Creates a plan + msg task in the DB so the CLI renders normally and
    ``/status`` works.  Delivers webhook if configured.

    Returns the plan_id (used by caller for post-plan usage tracking).

    .. note::

       Post-plan knowledge processing (curator, summarizer, fact
       consolidation) is handled by the caller after this returns,
       so chat-heavy sessions still trigger summarization.
    """
    deploy_secrets = collect_deploy_secrets(config)
    plan_id = await create_plan(db, session, msg_id, "Chat response")
    task_id = await create_task(db, plan_id, session, "msg", content)
    await update_task(db, task_id, "running")
    await update_task_substatus(db, task_id, "composing")

    # Store classifier usage on the plan header
    classifier_usage = get_usage_since(0)
    if classifier_usage["input_tokens"] or classifier_usage["output_tokens"]:
        await update_plan_usage(
            db, plan_id,
            classifier_usage["input_tokens"], classifier_usage["output_tokens"],
            classifier_usage["model"],
            llm_calls=classifier_usage.get("calls"),
        )

    usage_idx_before = get_usage_index()
    t0 = time.perf_counter()
    _msg_timeout = int(config.settings["planner_timeout"])
    try:
        try:
            text = await asyncio.wait_for(
                _msg_task(config, db, session, content, goal=content),
                timeout=_msg_timeout,
            )
        except asyncio.TimeoutError:
            raise MessengerError(f"Messenger timed out after {_msg_timeout}s")
    except (LLMError, MessengerError) as e:
        task_duration_ms = int((time.perf_counter() - t0) * 1000)
        log.error("Fast path messenger failed: %s", e)
        if slog:
            slog.info("Fast path failed: %s", e)
        error_text = f"Chat response failed: {e}"
        # Persist any partial LLM calls collected before the failure so verbose
        # panels still show the attempted messenger call.
        await _append_calls(db, task_id, usage_idx_before)
        await update_task(db, task_id, "failed", output=error_text)
        await update_plan_status(db, plan_id, "failed")
        audit.log_task(
            session, task_id, "msg", content, "failed", task_duration_ms, 0,
            deploy_secrets=deploy_secrets,
        )
        await save_message(
            db, session, None, "system", error_text,
            trusted=True, processed=True,
        )
        return plan_id

    task_duration_ms = int((time.perf_counter() - t0) * 1000)
    await update_task(db, task_id, "done", output=text)
    await update_plan_status(db, plan_id, "done")

    audit.log_task(
        session, task_id, "msg", content, "done", task_duration_ms,
        len(text), deploy_secrets=deploy_secrets,
    )

    # Append messenger call immediately (incremental rendering)
    await _append_calls(db, task_id, usage_idx_before)

    # Store messenger token totals
    step_usage = get_usage_since(usage_idx_before)
    await update_task_usage(
        db, task_id,
        step_usage["input_tokens"], step_usage["output_tokens"],
    )

    # Save assistant response to conversation history
    await save_message(
        db, session, None, "assistant", text,
        trusted=True, processed=True,
    )

    # Webhook delivery
    await _deliver_webhook_if_configured(
        db, config, session, task_id, text, True,
        deploy_secrets=deploy_secrets,
    )

    if slog:
        slog.info("Fast path done: chat response delivered (%dms)", task_duration_ms)

    return plan_id


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
    max_output_size = int(config.settings["max_output_size"])
    max_worker_retries = int(config.settings["max_worker_retries"])
    # Cache installed skills for the whole plan execution (avoid rescanning per task)
    installed_skills = discover_skills()

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

        usage_idx_before = get_usage_index()

        if task_type == "exec":
            # Write plan_outputs.json before execution
            _write_plan_outputs(session, plan_outputs)

            retry_context = ""
            exec_retries = 0

            while True:
                await update_task_substatus(db, task_id, "translating")
                # Translate natural-language detail → shell command
                sys_env = get_system_env(config)
                sys_env_text = build_system_env_section(sys_env, session=session)
                outputs_text = _format_plan_outputs_for_msg(plan_outputs)
                idx_translate = get_usage_index()
                try:
                    command = await run_exec_translator(
                        config, detail, sys_env_text,
                        plan_outputs_text=outputs_text, session=session,
                        retry_context=retry_context,
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

                # Append translator call immediately (incremental rendering)
                await _append_calls(db, task_id, idx_translate)
                await update_task_command(db, task_id, command)

                if slog:
                    slog.info("Task %d translated: %s → %s", task_id, detail[:80], command[:120])

                await update_task_substatus(db, task_id, "executing")
                t0 = time.perf_counter()
                stdout, stderr, success = await _exec_task(
                    session, command, exec_timeout, sandbox_uid=sandbox_uid,
                    max_output_size=max_output_size,
                )
                task_duration_ms = int((time.perf_counter() - t0) * 1000)
                stdout = sanitize_output(stdout, deploy_secrets, session_secrets or {})
                stderr = sanitize_output(stderr, deploy_secrets, session_secrets or {})
                status = "done" if success else "failed"

                # Report pub/ files if any were created
                pub_urls = _report_pub_files(session, config)
                if pub_urls:
                    pub_note = "\n\nPublished files:\n" + "\n".join(
                        f"- {u['filename']}: {u['url']}" for u in pub_urls
                    )
                    stdout += pub_note

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

                # Accumulate plan output (replace on retry)
                plan_output_entry = {
                    "index": i + 1,
                    "type": "exec",
                    "detail": detail,
                    "output": stdout,
                    "status": status,
                }
                if exec_retries > 0 and plan_outputs and plan_outputs[-1]["index"] == i + 1:
                    plan_outputs[-1] = plan_output_entry
                else:
                    plan_outputs.append(plan_output_entry)
                _write_plan_outputs(session, plan_outputs)

                # Review exec tasks
                await update_task_substatus(db, task_id, "reviewing")
                idx_review = get_usage_index()
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

                # Append reviewer call immediately (incremental rendering)
                await _append_calls(db, task_id, idx_review)

                if review["status"] == "replan":
                    retry_hint = review.get("retry_hint")
                    if retry_hint and exec_retries < max_worker_retries:
                        exec_retries += 1
                        await update_task_retry_count(db, task_id, exec_retries)
                        retry_context = (
                            f"Attempt {exec_retries} failed.\n"
                            f"Command: {command}\n"
                            f"Output: {stdout[:500]}\n"
                            f"Stderr: {stderr[:500]}\n"
                            f"Hint: {retry_hint}"
                        )
                        if slog:
                            slog.info("Task %d retry %d/%d: %s",
                                      task_id, exec_retries, max_worker_retries, retry_hint)
                        continue

                    # No hint or retries exhausted → escalate to full replan
                    replan_reason = review["reason"]
                    log.info("Reviewer requests replan: %s", replan_reason)
                    if slog:
                        if exec_retries > 0:
                            slog.info("Review → replan (retried %dx before escalating): %s",
                                      exec_retries, replan_reason)
                        else:
                            slog.info("Review → replan: %s", replan_reason)
                    # Store per-step token totals even on replan
                    step_usage = get_usage_since(usage_idx_before)
                    await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])
                    remaining = [dict(t) for t in tasks[i + 1:]]
                    _cleanup_plan_outputs(session)
                    return False, replan_reason, completed, remaining

                # review ok → break out of retry loop
                break

            # Store per-step token totals (translator + exec + reviewer)
            step_usage = get_usage_since(usage_idx_before)
            await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])

            if slog:
                slog.info("Review → %s", review["status"])
            completed.append(task_row)

        elif task_type == "msg":
            try:
                await update_task_substatus(db, task_id, "composing")
                t0 = time.perf_counter()
                _msg_timeout = int(config.settings["planner_timeout"])
                idx_msg = get_usage_index()
                try:
                    text = await asyncio.wait_for(
                        _msg_task(
                            config, db, session, detail,
                            plan_outputs=plan_outputs,
                            goal=goal,
                        ),
                        timeout=_msg_timeout,
                    )
                except asyncio.TimeoutError:
                    raise MessengerError(f"Messenger timed out after {_msg_timeout}s")
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
                is_final = i == len(tasks) - 1
                await _deliver_webhook_if_configured(
                    db, config, session, task_id, text, is_final,
                    deploy_secrets=deploy_secrets,
                    session_secrets=session_secrets,
                )

                # Append messenger call immediately (incremental rendering)
                await _append_calls(db, task_id, idx_msg)

                # Store per-step token totals (messenger)
                step_usage = get_usage_since(usage_idx_before)
                await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])

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

            # Look up the skill (from plan-level cache)
            skill_info = next((s for s in installed_skills if s["name"] == skill_name), None)

            t0 = time.perf_counter()

            # Pre-flight checks: skill installed, args valid
            setup_error: str | None = None
            if skill_info is None:
                setup_error = f"Skill '{skill_name}' not installed"
            else:
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError as e:
                    setup_error = f"Invalid skill args JSON: {e}"
                    args = None
                else:
                    validation_errors = validate_skill_args(args, skill_info["args_schema"])
                    if validation_errors:
                        setup_error = "Skill args validation failed: " + "; ".join(validation_errors)

            if setup_error:
                log.error("Skill setup failed for task %d: %s", task_id, setup_error)
                await update_task(db, task_id, "failed", output=setup_error)
                audit.log_task(
                    session, task_id, "skill", detail, "failed", 0, 0,
                    deploy_secrets=deploy_secrets,
                    session_secrets=session_secrets or {},
                )
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

            # Write plan_outputs.json before skill execution
            _write_plan_outputs(session, plan_outputs)

            await update_task_substatus(db, task_id, "executing")
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
            await update_task_substatus(db, task_id, "reviewing")
            idx_review = get_usage_index()
            try:
                review = await _review_task(
                    config, db, session, goal, task_row, user_message,
                )
            except ReviewError as e:
                log.error("Review failed for skill task %d: %s", task_id, e)
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

            # Append reviewer call immediately (incremental rendering)
            await _append_calls(db, task_id, idx_review)

            if review["status"] == "replan":
                replan_reason = review["reason"]
                if slog:
                    slog.info("Review → replan: %s", replan_reason)
                step_usage = get_usage_since(usage_idx_before)
                await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, replan_reason, completed, remaining

            # Store per-step token totals (skill + reviewer)
            step_usage = get_usage_since(usage_idx_before)
            await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])

            if slog:
                slog.info("Review → %s", review["status"])

            if task_row["status"] == "failed":
                remaining = [dict(t) for t in tasks[i + 1:]]
                _cleanup_plan_outputs(session)
                return False, None, completed, remaining

            completed.append(task_row)

        elif task_type == "search":
            search_retries = 0
            search_extra_context = ""

            while True:
                t0 = time.perf_counter()
                await update_task_substatus(db, task_id, "searching")
                idx_search = get_usage_index()
                try:
                    outputs_text = _format_plan_outputs_for_msg(plan_outputs)
                    full_context = outputs_text
                    if search_extra_context:
                        full_context = (full_context + "\n\n" + search_extra_context).strip()
                    search_result = await _search_task(
                        config, detail, task_row.get("args"),
                        context=full_context,
                        session=session,
                        task_id=task_id,
                    )
                except SearcherError as e:
                    task_duration_ms = int((time.perf_counter() - t0) * 1000)
                    log.error("Search failed for task %d: %s", task_id, e)
                    await update_task(db, task_id, "failed", output=str(e))
                    audit.log_task(
                        session, task_id, "search", detail, "failed", task_duration_ms, 0,
                        deploy_secrets=deploy_secrets,
                        session_secrets=session_secrets or {},
                    )
                    remaining = [dict(t) for t in tasks[i + 1:]]
                    _cleanup_plan_outputs(session)
                    return False, None, completed, remaining

                task_duration_ms = int((time.perf_counter() - t0) * 1000)
                await update_task(db, task_id, "done", output=search_result)
                task_row = {**task_row, "output": search_result, "status": "done"}

                audit.log_task(
                    session, task_id, "search", detail, "done", task_duration_ms,
                    len(search_result), deploy_secrets=deploy_secrets,
                    session_secrets=session_secrets or {},
                )
                if slog:
                    slog.info("Task %d done: [search] done (%dms)", task_id, task_duration_ms)

                # Append searcher call immediately (incremental rendering)
                await _append_calls(db, task_id, idx_search)

                # Accumulate plan output (replace on retry)
                plan_output_entry = {
                    "index": i + 1,
                    "type": "search",
                    "detail": detail,
                    "output": search_result,
                    "status": "done",
                }
                if search_retries > 0 and plan_outputs and plan_outputs[-1]["index"] == i + 1:
                    plan_outputs[-1] = plan_output_entry
                else:
                    plan_outputs.append(plan_output_entry)

                # Review search tasks
                await update_task_substatus(db, task_id, "reviewing")
                idx_review = get_usage_index()
                try:
                    review = await _review_task(
                        config, db, session, goal, task_row, user_message,
                    )
                except ReviewError as e:
                    log.error("Review failed for search task %d: %s", task_id, e)
                    remaining = [dict(t) for t in tasks[i + 1:]]
                    _cleanup_plan_outputs(session)
                    return False, None, completed, remaining

                # Append reviewer call immediately (incremental rendering)
                await _append_calls(db, task_id, idx_review)

                if review["status"] == "replan":
                    retry_hint = review.get("retry_hint")
                    if retry_hint and search_retries < max_worker_retries:
                        search_retries += 1
                        await update_task_retry_count(db, task_id, search_retries)
                        search_extra_context += (
                            f"\n\n[Retry {search_retries}] Previous search was insufficient. "
                            f"Hint: {retry_hint}"
                        )
                        if slog:
                            slog.info("Task %d search retry %d/%d: %s",
                                      task_id, search_retries, max_worker_retries, retry_hint)
                        continue

                    # No hint or retries exhausted → escalate to full replan
                    replan_reason = review["reason"]
                    if slog:
                        if search_retries > 0:
                            slog.info("Review → replan (retried %dx before escalating): %s",
                                      search_retries, replan_reason)
                        else:
                            slog.info("Review → replan: %s", replan_reason)
                    step_usage = get_usage_since(usage_idx_before)
                    await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])
                    remaining = [dict(t) for t in tasks[i + 1:]]
                    _cleanup_plan_outputs(session)
                    return False, replan_reason, completed, remaining

                # review ok → break out of retry loop
                break

            # Store per-step token totals (searcher + reviewer)
            step_usage = get_usage_since(usage_idx_before)
            await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])

            if slog:
                slog.info("Review → %s", review["status"])
            completed.append(task_row)

        elif task_type == "replan":
            # Self-directed replan: mark task as done, trigger replan
            replan_detail = task_row["detail"]
            await update_task(db, task_id, "done", output="Replan requested by planner")
            task_row = {**task_row, "output": "Replan requested by planner", "status": "done"}
            completed.append(task_row)
            remaining = [dict(t) for t in tasks[i + 1:]]
            _cleanup_plan_outputs(session)
            if slog:
                slog.info("Task %d: self-directed replan: %s", task_id, replan_detail[:120])
            return False, f"Self-directed replan: {replan_detail}", completed, remaining

    _cleanup_plan_outputs(session)
    return True, None, completed, []


async def _apply_curator_result(
    db: aiosqlite.Connection, session: str, result: dict
) -> None:
    """Apply curator evaluations: promote facts, create pending questions, discard."""
    for ev in result.get("evaluations", []):
        lid = ev["learning_id"]
        verdict = ev["verdict"]
        if verdict == "promote":
            category = ev.get("category") or "general"
            fact_session = session if category == "user" else None
            await save_fact(db, ev["fact"], source="curator", session=fact_session, category=category)
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
    idle_timeout = int(config.settings["worker_idle_timeout"])
    exec_timeout = int(config.settings["exec_timeout"])
    planner_timeout = int(config.settings["planner_timeout"])
    max_replan_depth = int(config.settings["max_replan_depth"])
    slog = SessionLogger(session, base_dir=KISO_DIR)

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
                                       idle_timeout, exec_timeout, planner_timeout,
                                       max_replan_depth, slog=slog)
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
    planner_timeout: int,
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
    max_llm_calls = int(config.settings["max_llm_calls_per_message"])
    set_llm_budget(max_llm_calls)
    reset_usage_tracking()

    await mark_message_processed(db, msg_id)

    # --- Fast path: skip planner for conversational messages ---
    # Paraphraser is intentionally skipped here — the messenger only sees
    # session summary + facts + the current user message (all trusted).
    # Untrusted messages feed into planner context, not messenger context.
    fast_path_enabled = setting_bool(config.settings, "fast_path_enabled")
    if fast_path_enabled:
        msg_class = await classify_message(config, content, session=session)
        if msg_class == "chat":
            log.info("Fast path: chat message, skipping planner")
            if slog:
                slog.info("Fast path: classified as chat, skipping planner")
            fast_plan_id = await _fast_path_chat(
                db, config, session, msg_id, content, slog=slog,
            )
            # Bump fact usage for fast path (facts contributed to chat response)
            all_facts = await get_facts(db)
            if all_facts:
                await update_fact_usage(db, [f["id"] for f in all_facts])
            # Run post-plan knowledge processing (summarizer, curator, etc.)
            await _post_plan_knowledge(
                db, config, session, fast_plan_id, exec_timeout,
            )
            clear_llm_budget()
            return

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
        plan = await asyncio.wait_for(
            run_planner(
                db, config, session, user_role, content,
                user_skills=user_skills,
                paraphrased_context=paraphrased_context,
            ),
            timeout=planner_timeout,
        )
    except asyncio.TimeoutError:
        log.error("Planner timed out after %ds for session=%s msg=%d", planner_timeout, session, msg_id)
        error_text = f"Planning timed out after {planner_timeout}s"
        fail_plan_id = await create_plan(db, session, msg_id, "Failed")
        fail_task_id = await create_task(db, fail_plan_id, session, "msg", error_text)
        await update_task(db, fail_task_id, status="done", output=error_text)
        await update_plan_status(db, fail_plan_id, "failed")
        await save_message(db, session, None, "system", error_text, trusted=True, processed=True)
        await _deliver_webhook_if_configured(
            db, config, session, 0, error_text, True,
            deploy_secrets=collect_deploy_secrets(config),
        )
        return
    except PlanError as e:
        log.error("Planning failed session=%s msg=%d: %s", session, msg_id, e)
        error_text = f"Planning failed: {e}"
        # Create a failed plan + msg task so the CLI can detect the failure
        fail_plan_id = await create_plan(db, session, msg_id, "Failed")
        fail_task_id = await create_task(db, fail_plan_id, session, "msg", error_text)
        await update_task(db, fail_task_id, status="done", output=error_text)
        await update_plan_status(db, fail_plan_id, "failed")
        await save_message(db, session, None, "system", error_text, trusted=True, processed=True)
        deploy_secrets = collect_deploy_secrets(config)
        await _deliver_webhook_if_configured(
            db, config, session, 0, error_text, True,
            deploy_secrets=deploy_secrets,
        )
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

    # Store planner usage immediately so the CLI can display it with the plan header
    planner_usage = get_usage_since(0)
    if planner_usage["input_tokens"] or planner_usage["output_tokens"]:
        await update_plan_usage(
            db, plan_id,
            planner_usage["input_tokens"], planner_usage["output_tokens"],
            planner_usage["model"],
            llm_calls=planner_usage.get("calls"),
        )

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
            # Bump fact usage for all facts (they contributed to a successful plan)
            all_facts = await get_facts(db)
            if all_facts:
                await update_fact_usage(db, [f["id"] for f in all_facts])
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
                    goal=current_goal,
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
            await _deliver_webhook_if_configured(
                db, config, session, 0, cancel_text, True,
                deploy_secrets=collect_deploy_secrets(config),
                session_secrets=session_secrets,
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
                fail_text = await _msg_task(config, db, session, fail_detail,
                                            goal=current_goal)
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
            await _deliver_webhook_if_configured(
                db, config, session, 0, fail_text, True,
                deploy_secrets=collect_deploy_secrets(config),
                session_secrets=session_secrets,
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
                replan_text = await _msg_task(config, db, session, replan_detail,
                                              goal=current_goal)
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
            await _deliver_webhook_if_configured(
                db, config, session, 0, replan_text, True,
                deploy_secrets=collect_deploy_secrets(config),
                session_secrets=session_secrets,
            )
            break

        # Detect self-directed replan
        is_self_directed = replan_reason.startswith("Self-directed replan:")

        # Set "replanning" status so the CLI knows to keep polling
        await update_plan_status(db, current_plan_id, "replanning")

        # Mark remaining tasks
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

        # Notify user about replan (as a visible msg task + webhook)
        if is_self_directed:
            msg_text = f"Investigating... ({replan_depth}/{max_replan_depth})"
        else:
            msg_text = (
                f"Replanning (attempt {replan_depth}/{max_replan_depth}): "
                f"{replan_reason}"
            )
        replan_notify_id = await create_task(
            db, current_plan_id, session, "msg", msg_text,
        )
        await update_task(db, replan_notify_id, status="done", output=msg_text)
        await save_message(
            db, session, None, "system", msg_text,
            trusted=True, processed=True,
        )
        await _deliver_webhook_if_configured(
            db, config, session, replan_notify_id, msg_text, False,
            deploy_secrets=collect_deploy_secrets(config),
            session_secrets=session_secrets,
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

        replan_usage_idx = get_usage_index()
        try:
            new_plan = await asyncio.wait_for(
                run_planner(
                    db, config, session, user_role, enriched_message,
                    user_skills=user_skills,
                ),
                timeout=planner_timeout,
            )
        except asyncio.TimeoutError:
            log.error("Replan timed out after %ds", planner_timeout)
            e = f"Replan timed out after {planner_timeout}s"
            await update_plan_status(db, current_plan_id, "failed")
            fail_detail = _build_failure_summary(
                completed, remaining, current_goal,
                reason=f"Replan timed out: {e}",
            )
            try:
                fail_text = await _msg_task(config, db, session, fail_detail,
                                            goal=current_goal)
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
            break
        except PlanError as e:
            log.error("Replan failed: %s", e)
            # Finalize plan status from "replanning" to "failed"
            await update_plan_status(db, current_plan_id, "failed")
            # Recovery msg task so the CLI displays feedback to the user
            fail_detail = _build_failure_summary(
                completed, remaining, current_goal,
                reason=f"Replan failed: {e}",
            )
            try:
                fail_text = await _msg_task(config, db, session, fail_detail,
                                            goal=current_goal)
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
            break

        # Finalize old plan status from "replanning" to its final state
        if is_self_directed:
            await update_plan_status(db, current_plan_id, "done")
        else:
            await update_plan_status(db, current_plan_id, "failed")

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

        # Store replanner usage immediately
        replan_planner_usage = get_usage_since(replan_usage_idx)
        if replan_planner_usage["input_tokens"] or replan_planner_usage["output_tokens"]:
            await update_plan_usage(
                db, new_plan_id,
                replan_planner_usage["input_tokens"],
                replan_planner_usage["output_tokens"],
                replan_planner_usage["model"],
                llm_calls=replan_planner_usage.get("calls"),
            )

        # Handle extend_replan: planner can request up to +3 extra attempts
        extend = new_plan.get("extend_replan")
        if extend and isinstance(extend, int) and extend > 0:
            extend = min(extend, 3)  # cap at +3
            max_replan_depth += extend
            log.info("Planner requested %d extra replan attempts (new limit: %d)",
                     extend, max_replan_depth)

        current_plan_id = new_plan_id
        current_goal = new_plan["goal"]

    # --- Store token usage on the plan (before post-plan processing) ---
    # Only update totals; llm_calls is preserved (planner-only, set earlier).
    usage = get_usage_summary()
    if current_plan_id and (usage["input_tokens"] or usage["output_tokens"]):
        await update_plan_usage(
            db, current_plan_id,
            usage["input_tokens"], usage["output_tokens"], usage["model"],
        )

    # --- Invalidate system env cache (exec tasks may have changed the system) ---
    from kiso.sysenv import invalidate_cache
    invalidate_cache()

    # --- Post-plan knowledge processing ---
    await _post_plan_knowledge(db, config, session, current_plan_id, exec_timeout)

    clear_llm_budget()
