"""Session loop, plan orchestration, and message processing for the kiso worker."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

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
    CURATOR_VERDICT_ASK,
    CURATOR_VERDICT_DISCARD,
    CURATOR_VERDICT_PROMOTE,
    REVIEW_STATUS_REPLAN,
    REVIEW_STATUS_STUCK,
    TASK_TYPE_EXEC,
    TASK_TYPE_MSG,
    TASK_TYPE_REPLAN,
    TASK_TYPE_SEARCH,
    TASK_TYPE_TOOL,
    WORKER_PHASE_CLASSIFYING,
    WORKER_PHASE_EXECUTING,
    WORKER_PHASE_IDLE,
    WORKER_PHASE_PLANNING,
    _MAX_MESSENGER_FACTS,
    BrieferError,
    ClassifierError,
    CuratorError,
    ExecTranslatorError,
    MessengerError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    classify_message,
    run_briefer,
    run_curator,
    run_exec_translator,
    run_fact_consolidation,
    run_messenger,
    run_paraphraser,
    run_planner,
    run_reviewer,
    run_summarizer,
    prepare_reviewer_output,
    clean_learn_items,
    _INSTALL_CMD_RE,
)
from kiso.config import Config, setting_bool, setting_float, setting_int
from kiso.llm import (
    LLMBudgetExceeded,
    LLMError,
    clear_llm_budget,
    get_usage_index,
    get_usage_since,
    get_usage_summary,
    reset_usage_tracking,
    set_llm_budget,
)
from kiso.tools import (
    ToolError,
    discover_tools,
    invalidate_tools_cache,
    auto_correct_tool_args,
    validate_tool_args,
)
from kiso.sysenv import get_system_env, build_system_env_section, invalidate_cache
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
    _normalize_entity_name,
    find_or_create_entity,
    get_all_entities,
    get_all_tags,
    search_facts_by_entity,
    search_facts_scored,
    get_facts,
    get_oldest_messages,
    get_safety_facts,
    get_pending_learnings,
    get_plan_for_session,
    get_session,
    get_tasks_for_plan,
    get_untrusted_messages,
    mark_message_processed,
    save_fact,
    save_facts_batch,
    save_learning,
    save_message,
    session_has_install_proposal,
    save_pending_item,
    search_facts,
    update_fact_usage,
    update_learning,
    update_plan_goal,
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

from kiso.worker.l10n import LANG_NAMES
from kiso.worker.utils import (
    _auto_publish_skill_files,
    _build_cancel_summary,
    _build_failure_summary,
    _build_replan_context,
    _check_disk_limit,
    _cleanup_plan_outputs,
    _ensure_sandbox_user,
    _format_plan_outputs_for_msg,
    _format_pub_note,
    _report_pub_files,
    _save_large_output,
    _session_workspace,
    _snapshot_workspace,
    _write_plan_outputs,
    detect_user_lang,
    get_replan_message,
)
from kiso.worker.exec import _exec_task
from kiso.worker.tool import _tool_task

log = logging.getLogger(__name__)

_MAX_EXTEND_REPLAN = 3  # maximum extra replan attempts the planner can request
_POST_INSTALL_RESCAN_DELAY: float = 3.0  # seconds to wait before rescan after tool install

# Task substatus labels written to the DB during execution
_SUBSTATUS_TRANSLATING = "translating"
_SUBSTATUS_EXECUTING = "executing"
_SUBSTATUS_REVIEWING = "reviewing"
_SUBSTATUS_COMPOSING = "composing"
_SUBSTATUS_SEARCHING = "searching"


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
        max_payload=setting_int(config.settings, "webhook_max_payload", lo=1),
    )
    audit.log_webhook(
        session, task_id, webhook_url, wh_status, wh_attempts,
        deploy_secrets=deploy_secrets or {},
        session_secrets=session_secrets or {},
    )


def _is_briefer_budget_ok(config: Config) -> bool:
    """Check if the briefer should run based on config and LLM budget."""
    if not setting_bool(config.settings, "briefer_enabled"):
        return False
    from kiso.llm import get_llm_call_count
    max_calls = setting_int(config.settings, "max_llm_calls_per_message", lo=1)
    if get_llm_call_count() >= max_calls - 2:
        log.debug("Skipping briefer: LLM budget near limit (%d/%d)",
                  get_llm_call_count(), max_calls)
        return False
    return True


async def _msg_task(
    config: Config,
    db: aiosqlite.Connection,
    session: str,
    detail: str,
    plan_outputs: list[dict] | None = None,
    goal: str = "",
    include_recent: bool = False,
    user_message: str = "",
    on_briefer_done: "Callable | None" = None,
) -> str:
    """Generate a user-facing message via the messenger brain role.

    When the briefer is enabled, it selects which plan_outputs are relevant
    to the msg task and synthesizes context, reducing noise in the messenger.

    Args:
        on_briefer_done: Optional async callback invoked after the briefer
            completes but before the messenger LLM call.  The caller can use
            this to flush intermediate usage so the CLI can render briefer
            panels while the messenger is still running (M273).
    """
    selected_outputs = plan_outputs
    briefing_context: str | None = None

    # M523/M582: skip briefer if LLM budget is nearly exhausted
    _briefer_ok = _is_briefer_budget_ok(config)

    if _briefer_ok:
        try:
            # Build full context pool for the briefer
            context_pool: dict = {}
            if plan_outputs:
                context_pool["plan_outputs"] = _format_plan_outputs_for_msg(plan_outputs)
            if goal:
                context_pool["goal"] = goal
            if user_message:
                context_pool["recent_messages"] = user_message
            # Fetch summary/facts for the briefer to filter
            sess = await get_session(db, session)
            if sess and sess["summary"]:
                context_pool["summary"] = sess["summary"]
            facts = await get_facts(db, session=session, limit=_MAX_MESSENGER_FACTS)
            if facts:
                context_pool["facts"] = "\n".join(f"- {f['content']}" for f in facts)
            # M385: inject available tags for briefer relevant_tags selection
            all_tags = await get_all_tags(db)
            if all_tags:
                context_pool["available_tags"] = ", ".join(all_tags)
            # M365: inject available entities so briefer can select relevant ones
            all_entities = await get_all_entities(db)
            if all_entities:
                context_pool["available_entities"] = "\n".join(
                    f"{e['name']} ({e['kind']})" for e in all_entities
                )

            briefing = await run_briefer(
                config, "messenger", detail, context_pool, session=session,
            )
            # Filter plan_outputs by selected indices
            if plan_outputs:
                indices = set(briefing.get("output_indices", []))
                if indices:
                    selected_outputs = [o for o in plan_outputs if o["index"] in indices]
                    if not selected_outputs:
                        selected_outputs = plan_outputs  # safety: don't pass empty
            # Use briefer's synthesized context instead of raw summary/facts
            if briefing.get("context"):
                briefing_context = briefing["context"]
            # M391: scored fact retrieval for messenger
            entity_id = None
            if briefing.get("relevant_entities") and all_entities:
                entity_map = {_normalize_entity_name(e["name"]): e["id"] for e in all_entities}
                for ename in briefing["relevant_entities"]:
                    eid = entity_map.get(_normalize_entity_name(ename))
                    if eid is not None:
                        entity_id = eid
                        break
            scored_facts = await search_facts_scored(
                db,
                entity_id=entity_id,
                tags=briefing.get("relevant_tags") or None,
                keywords=detail.lower().split()[:10] if detail else None,
            )
            if scored_facts:
                facts_text = "\n".join(f"- {f['content']}" for f in scored_facts)
                if briefing_context:
                    briefing_context += f"\n\n## Relevant Facts\n{facts_text}"
                else:
                    briefing_context = f"## Relevant Facts\n{facts_text}"
        except (BrieferError, LLMError, asyncio.TimeoutError):
            log.debug("Briefer failed for messenger, using full context")

    # M273: flush briefer usage before messenger call
    if on_briefer_done:
        await on_briefer_done()

    log.debug("Messenger detail: %s", detail[:120])
    outputs_text = _format_plan_outputs_for_msg(selected_outputs) if selected_outputs else ""
    return await run_messenger(
        db, config, session, detail, outputs_text, goal=goal,
        include_recent=include_recent, user_message=user_message,
        briefing_context=briefing_context,
    )


def _spawn_knowledge_task(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int | None,
    llm_timeout: int,
) -> asyncio.Task:
    """Spawn _post_plan_knowledge as a background task with its own LLM budget."""

    async def _run() -> None:
        max_calls = setting_int(config.settings, "max_llm_calls_per_message", lo=1)
        set_llm_budget(max_calls)
        reset_usage_tracking()
        try:
            await _post_plan_knowledge(db, config, session, plan_id, llm_timeout)
        except Exception:
            log.exception("Background post-plan knowledge failed for session=%s", session)
        finally:
            clear_llm_budget()

    return asyncio.create_task(_run())


async def _post_plan_knowledge(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int | None,
    llm_timeout: int,
) -> None:
    """Run post-plan knowledge processing: curator, summarizer, fact consolidation.

    Called after both normal and fast-path message processing to keep
    the knowledge base current regardless of which path was taken.

    Execution order:
      Phase 1 — Curator + Summarizer in parallel (independent LLM calls,
                 no shared data between them).
      Phase 2 — Fact consolidation (after Curator so promoted facts are visible).
      Phase 3 — Decay + Archive in parallel (pure SQL, no LLM dependency).
    """

    # --- Phase 1: Curator + Summarizer (independent, run concurrently) ----------

    async def _run_curator() -> None:
        learnings = await get_pending_learnings(db)
        if not learnings:
            return
        try:
            tags = await get_all_tags(db)
            entities = await get_all_entities(db)
            # M392/M505: scored fact retrieval for curator dedup
            # Build lowercase content cache once, then dict-lookup entities
            lower_contents = [l["content"].lower() for l in learnings]
            matched: list[dict] = [
                e for e in entities
                if any(e["name"] in lc for lc in lower_contents)
            ]
            # Batch entity fact queries concurrently
            if matched:
                results = await asyncio.gather(*(
                    search_facts_scored(db, entity_id=e["id"], limit=20)
                    for e in matched
                ))
                relevant_facts: list[dict] = []
                for entity, efacts in zip(matched, results):
                    for f in efacts:
                        f["entity_name"] = entity["name"]
                    relevant_facts.extend(efacts)
            else:
                relevant_facts = []
            curator_result = await asyncio.wait_for(
                run_curator(config, learnings, session=session,
                            available_tags=tags, available_entities=entities,
                            existing_facts=relevant_facts or None),
                timeout=llm_timeout,
            )
            await _apply_curator_result(db, session, curator_result)
            # M382: backfill entity_id for older facts matching newly created entities
            from kiso.store import backfill_fact_entities
            await backfill_fact_entities(db)
        except asyncio.TimeoutError:
            log.warning("Curator timed out after %ds", llm_timeout)
        except CuratorError as e:
            log.error("Curator failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected error in curator/apply phase for session=%s", session)

    async def _run_summarizer() -> None:
        msg_count = await count_messages(db, session)
        if msg_count < setting_int(config.settings, "summarize_threshold", lo=1):
            return
        try:
            sess = await get_session(db, session)
            current_summary = sess["summary"] if sess else ""
            msg_limit = setting_int(config.settings, "summarize_messages_limit", lo=1)
            oldest = await get_oldest_messages(db, session, limit=min(msg_count, msg_limit))
            new_summary = await asyncio.wait_for(
                run_summarizer(config, current_summary, oldest, session=session),
                timeout=llm_timeout,
            )
            await update_summary(db, session, new_summary)
        except asyncio.TimeoutError:
            log.warning("Summarizer timed out after %ds", llm_timeout)
        except SummarizerError as e:
            log.error("Summarizer failed: %s", e)

    await asyncio.gather(_run_curator(), _run_summarizer())

    # --- Phase 2: Fact consolidation (after Curator — sees promoted facts) ------

    max_facts = setting_int(config.settings, "knowledge_max_facts", lo=1)
    all_facts = await get_facts(db, is_admin=True)
    if len(all_facts) > max_facts:
        try:
            consolidated = await asyncio.wait_for(
                run_fact_consolidation(config, all_facts, session=session),
                timeout=llm_timeout,
            )
            if consolidated:
                min_ratio = setting_float(config.settings, "fact_consolidation_min_ratio", lo=0.0, hi=1.0)
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
                        # Preserve session scoping: user facts belong to the
                        # session that triggered consolidation; all other
                        # categories are global (session=None).
                        fact_rows = []
                        for f in consolidated:
                            category = f.get("category", "general")
                            fact_rows.append({
                                "content": f["content"],
                                "source": "consolidation",
                                "category": category,
                                "confidence": f.get("confidence", 1.0),
                                "session": session if category == "user" else None,
                            })
                        await save_facts_batch(db, fact_rows)
                    else:
                        log.warning("All consolidated facts filtered out, preserving originals")
        except asyncio.TimeoutError:
            log.warning("Fact consolidation timed out after %ds", llm_timeout)
        except SummarizerError as e:
            log.error("Fact consolidation failed: %s", e)

    # --- Phase 3: Decay + Archive (pure SQL, run concurrently) ------------------

    decay_days = setting_int(config.settings, "fact_decay_days", lo=1)
    decay_rate = setting_float(config.settings, "fact_decay_rate", lo=0.0, hi=1.0)
    archive_threshold = setting_float(config.settings, "fact_archive_threshold", lo=0.0, hi=1.0)

    async def _run_decay() -> None:
        try:
            decayed = await decay_facts(db, decay_days=decay_days, decay_rate=decay_rate)
            if decayed:
                log.info("Decayed %d stale facts", decayed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Fact decay failed: %s", e)

    async def _run_archive() -> None:
        try:
            archived = await archive_low_confidence_facts(db, threshold=archive_threshold)
            if archived:
                log.info("Archived %d low-confidence facts", archived)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Fact archiving failed: %s", e)

    await asyncio.gather(_run_decay(), _run_archive())

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
    messenger_timeout: int = 120,
    slog: SessionLogger | None = None,
    plan_id: int | None = None,
    user_lang: str = "en",
) -> int:
    """Fast path for chat messages: skip planner, go straight to messenger.

    Reuses an existing plan (if *plan_id* given) or creates a new one so the
    CLI renders normally and ``/status`` works.  Delivers webhook if configured.

    Returns the plan_id (used by caller for post-plan usage tracking).

    .. note::

       Post-plan knowledge processing (curator, summarizer, fact
       consolidation) is handled by the caller after this returns,
       so chat-heavy sessions still trigger summarization.
    """
    deploy_secrets = collect_deploy_secrets()
    if plan_id is None:
        plan_id = await create_plan(db, session, msg_id, "Chat response")
    else:
        await update_plan_goal(db, plan_id, "Chat response")
    task_id = await create_task(db, plan_id, session, TASK_TYPE_MSG, content)
    await update_task(db, task_id, "running")
    await update_task_substatus(db, task_id, _SUBSTATUS_COMPOSING)

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
    idx_after_briefer = [usage_idx_before]  # mutated by callback

    async def _flush_briefer():
        """Flush briefer calls so CLI renders panels before messenger runs."""
        await _append_calls(db, task_id, usage_idx_before)
        idx_after_briefer[0] = get_usage_index()

    # Prepend "Answer in {lang}." so the messenger responds in the right language
    lang_name = LANG_NAMES.get(user_lang, "English")
    detail = f"Answer in {lang_name}. {content}"

    t0 = time.perf_counter()
    try:
        try:
            text = await asyncio.wait_for(
                _msg_task(config, db, session, detail, goal=content,
                          include_recent=True,
                          user_message=content,
                          on_briefer_done=_flush_briefer),
                timeout=messenger_timeout,
            )
        except asyncio.TimeoutError:
            raise MessengerError(f"Messenger timed out after {messenger_timeout}s")
    except (LLMError, MessengerError) as e:
        task_duration_ms = int((time.perf_counter() - t0) * 1000)
        log.error("Fast path messenger failed: %s", e)
        if slog:
            slog.info("Fast path failed: %s", e)
        error_text = f"Chat response failed: {e}"
        # Persist any partial LLM calls collected before the failure so verbose
        # panels still show the attempted messenger call.
        await _append_calls(db, task_id, idx_after_briefer[0])
        await update_task(db, task_id, "failed", output=error_text, duration_ms=task_duration_ms)
        await update_plan_status(db, plan_id, "failed")
        audit.log_task(
            session, task_id, TASK_TYPE_MSG, content, "failed", task_duration_ms, 0,
            deploy_secrets=deploy_secrets,
        )
        await save_message(
            db, session, None, "system", error_text,
            trusted=True, processed=True,
        )
        return plan_id

    task_duration_ms = int((time.perf_counter() - t0) * 1000)
    await update_task(db, task_id, "done", output=text, duration_ms=task_duration_ms)
    await update_plan_status(db, plan_id, "done")

    audit.log_task(
        session, task_id, TASK_TYPE_MSG, content, "done", task_duration_ms,
        len(text), deploy_secrets=deploy_secrets,
    )

    # Append messenger call immediately (incremental rendering)
    await _append_calls(db, task_id, idx_after_briefer[0])

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


_ANSWER_IN_RE = re.compile(r"^Answer in \w[\w\s]*\.\s*", re.IGNORECASE)


def _maybe_inject_intent_msg(tasks: list[dict], goal: str) -> list[dict]:
    """M201: prepend an intent msg so the user knows what's about to happen.

    Skips injection when: plan has ≤1 task or first task is already a msg.
    Propagates the ``Answer in {language}.`` prefix from existing msg tasks.
    Returns a new list (does not mutate the input).
    """
    if len(tasks) <= 1 or tasks[0]["type"] == TASK_TYPE_MSG:
        return tasks

    # Extract "Answer in {language}." from existing msg tasks if available
    lang_prefix = ""
    for t in tasks:
        if t["type"] == TASK_TYPE_MSG:
            detail = t.get("detail") or ""
            m = _ANSWER_IN_RE.match(detail)
            if m:
                lang_prefix = m.group(0)
                break
            # Legacy [Lang: xx] support during transition
            if detail.startswith("[Lang:") and "]" in detail:
                lang_prefix = detail[:detail.index("]") + 1] + " "
                break

    task_summary = ", ".join(
        f"{t['type']}: {t['detail'][:60]}" for t in tasks[:3]
    )
    intent_task = {
        "type": TASK_TYPE_MSG,
        "detail": (
            f"{lang_prefix}Briefly tell the user what the system is about to do. "
            f"Plan: {task_summary}"
        ),
        "tool": None,
        "args": None,
        "expect": None,
    }
    return [intent_task] + tasks


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
            skill=t.get("tool"), args=t.get("args"), expect=t.get("expect"),
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
    """Review an exec/tool task. Returns review dict. Stores learning if present."""
    output = task_row.get("output") or ""
    stderr = task_row.get("stderr") or ""
    full_output = prepare_reviewer_output(output, stderr)

    # M412: fetch safety rules for compliance check
    safety_facts = await get_safety_facts(db)
    safety_rules = [f["content"] for f in safety_facts] if safety_facts else None

    success = task_row.get("status") == "done"
    exit_code = task_row.get("exit_code")
    review = await run_reviewer(
        config,
        goal=goal,
        detail=task_row["detail"],
        expect=task_row["expect"] or "",
        output=full_output,
        user_message=user_message,
        session=session,
        success=success,
        exit_code=exit_code,
        safety_rules=safety_rules,
    )

    learn_raw = review.get("learn")
    if isinstance(learn_raw, list):
        learn_items = learn_raw
    elif isinstance(learn_raw, str):
        log.warning("Reviewer returned learn as string, expected list; wrapping: %r", learn_raw[:100])
        learn_items = [learn_raw]
    else:
        learn_items = []
    # Discard learnings when output is empty — reviewer may hallucinate
    if not (output.strip() or stderr.strip()):
        if learn_items:
            log.warning("Discarding %d learning(s) for task %d — empty output",
                        len(learn_items), task_row.get("id", 0))
        learn_items = []
    # Filter low-quality items (M320, M373)
    learn_items = clean_learn_items(learn_items, task_output=full_output)
    has_learning = bool(learn_items)
    for item in learn_items:
        await save_learning(db, item, session)
        log.debug("Learning saved: %s", item[:100])

    audit.log_review(session, task_row.get("id", 0), review["status"], has_learning)

    await update_task_review(
        db, task_row["id"], review["status"],
        reason=review.get("reason"),
        learning="\n".join(learn_items) if learn_items else None,
    )

    return review


# ---------------------------------------------------------------------------
# M62a/62b: Dispatch pattern and handler infrastructure
# ---------------------------------------------------------------------------

@dataclass
class _PlanCtx:
    """Shared execution context for task handlers within _execute_plan."""

    db: aiosqlite.Connection
    config: Config
    session: str
    goal: str
    user_message: str
    deploy_secrets: dict[str, str]
    session_secrets: dict[str, str]
    max_output_size: int
    max_worker_retries: int
    messenger_timeout: int
    installed_tools: list[dict]
    slog: "SessionLogger | None"
    sandbox_uid: "int | None"
    base_url: str = ""
    plan_outputs: list[dict] = field(default_factory=list)  # mutated in place by handlers
    # Derived from installed_tools for O(1) lookup by name (populated in __post_init__)
    installed_tools_by_name: dict[str, dict] = field(init=False)

    def __post_init__(self) -> None:
        self.installed_tools_by_name = {s["name"]: s for s in self.installed_tools}


@dataclass
class _TaskHandlerResult:
    """Outcome from a task handler in _execute_plan."""

    stop: bool = False           # True → return early from _execute_plan
    stop_success: bool = False   # success value when stop=True
    stop_replan: "str | None" = None   # replan reason when stop=True
    stop_stuck: "str | None" = None    # stuck reason when stop=True (no replan)
    completed_row: "dict | None" = None  # if set, append to completed
    plan_output: "dict | None" = None   # if set, append to ctx.plan_outputs


def _make_plan_output(
    index: int, task_type: str, detail: str, output: str, status: str,
    session: str = "",
) -> dict:
    """Build a plan-output entry dict (shared by all task handlers).

    When *session* is provided and *output* exceeds the large-output threshold,
    the full output is saved to a workspace file and replaced with a reference.
    """
    if session:
        output = _save_large_output(session, index, output)
    return {"index": index, "type": task_type, "detail": detail, "output": output, "status": status}


async def _fail_task_and_audit(
    ctx: _PlanCtx, task_id: int, task_type: str, detail: str,
    error: str, task_idx: int, *,
    replan_reason: str | None = None,
    output: str | None = None, stderr: str | None = None,
) -> _TaskHandlerResult:
    """Handle a task failure: update status, audit, build plan output, return stop result.

    *output* defaults to *error*, *stderr* defaults to None.
    *replan_reason* defaults to *error*.
    """
    log.error("Task %d %s failed: %s", task_id, task_type, error)
    await update_task(
        ctx.db, task_id, "failed",
        output=output if output is not None else error,
        stderr=stderr,
    )
    _audit_task(ctx, task_id, task_type, detail, "failed", 0)
    plan_output = _make_plan_output(
        task_idx, task_type, detail, error, "failed", session=ctx.session,
    )
    return _TaskHandlerResult(
        stop=True, stop_success=False,
        stop_replan=replan_reason or error,
        plan_output=plan_output,
    )


def _sanitize_task_output(
    stdout: str, stderr: str, ctx: _PlanCtx,
) -> tuple[str, str]:
    """Sanitize both stdout and stderr against deploy/session secrets."""
    return (
        sanitize_output(stdout, ctx.deploy_secrets, ctx.session_secrets),
        sanitize_output(stderr, ctx.deploy_secrets, ctx.session_secrets),
    )


def _log_task_done(ctx: _PlanCtx, task_id: int, task_type: str, status: str, duration_ms: int) -> None:
    """Log task completion if session logger is available."""
    if ctx.slog:
        ctx.slog.info("Task %d done: [%s] %s (%dms)", task_id, task_type, status, duration_ms)


def _audit_task(
    ctx: _PlanCtx,
    task_id: int,
    task_type: str,
    detail: str,
    status: str,
    duration_ms: int,
    output_len: int = 0,
) -> None:
    """Single audit logging point for task results."""
    audit.log_task(
        ctx.session, task_id, task_type, detail, status, duration_ms, output_len,
        deploy_secrets=ctx.deploy_secrets, session_secrets=ctx.session_secrets,
    )


def _notify_phase(set_phase: Callable[[str], None] | None, phase: str) -> None:
    """Notify phase change if callback is provided."""
    if set_phase is not None:
        set_phase(phase)


async def _store_step_usage(
    db: aiosqlite.Connection, task_id: int, usage_idx_before: int
) -> None:
    """Store per-step token totals on the task row."""
    step_usage = get_usage_since(usage_idx_before)
    await update_task_usage(db, task_id, step_usage["input_tokens"], step_usage["output_tokens"])


async def _handle_review_error(
    ctx: _PlanCtx, task_id: int, review_error: str,
    plan_output: dict, usage_idx_before: int,
) -> _TaskHandlerResult:
    """Store usage and return a stop result when review fails."""
    await _store_step_usage(ctx.db, task_id, usage_idx_before)
    return _TaskHandlerResult(
        stop=True, stop_success=False,
        stop_replan=f"Review failed: {review_error}",
        plan_output=plan_output,
    )


async def _run_review_step(
    ctx: _PlanCtx, task_row: dict
) -> "tuple[dict | None, str | None]":
    """Run the reviewer LLM step and append calls.

    Returns (review_dict, error_str). On ReviewError, review_dict is None and
    error_str is the error message; on success, error_str is None.
    """
    task_id = task_row["id"]
    await update_task_substatus(ctx.db, task_id, _SUBSTATUS_REVIEWING)
    idx = get_usage_index()
    try:
        review = await _review_task(
            ctx.config, ctx.db, ctx.session, ctx.goal, task_row, ctx.user_message
        )
    except ReviewError as e:
        log.error("Review failed for task %d: %s", task_id, e)
        return None, str(e)
    await _append_calls(ctx.db, task_id, idx)
    # Attach reviewer summary for downstream context (M146, M247)
    summary = review.get("summary")
    if summary:
        task_row["reviewer_summary"] = summary[:1500]
    return review, None


async def _handle_replan_task(
    ctx: _PlanCtx, task_row: dict, i: int, is_final: bool, usage_idx_before: int,
) -> _TaskHandlerResult:
    """Handle a self-directed replan task."""
    task_id = task_row["id"]
    detail = task_row["detail"]
    await update_task(ctx.db, task_id, "done", output="Replan requested by planner")
    task_row = {**task_row, "output": "Replan requested by planner", "status": "done"}
    if ctx.slog:
        ctx.slog.info("Task %d: self-directed replan: %s", task_id, detail[:120])
    return _TaskHandlerResult(
        stop=True,
        stop_success=False,
        stop_replan=f"Self-directed replan: {detail}",
        completed_row=task_row,
    )


async def _handle_msg_task(
    ctx: _PlanCtx, task_row: dict, i: int, is_final: bool, usage_idx_before: int,
) -> _TaskHandlerResult:
    """Handle a msg task (messenger LLM → user-facing text)."""
    task_id = task_row["id"]
    detail = task_row["detail"]
    t0 = time.perf_counter()
    idx_msg = get_usage_index()
    idx_after_briefer = [idx_msg]  # mutated by callback
    text: str | None = None
    try:
        await update_task_substatus(ctx.db, task_id, _SUBSTATUS_COMPOSING)
        idx_msg = get_usage_index()
        # Track index after briefer flush so post-messenger flush
        # doesn't re-append briefer calls (M273 fix).
        idx_after_briefer = [idx_msg]

        async def _flush_briefer():
            """M273: flush briefer calls so CLI renders panels before messenger runs."""
            await _append_calls(ctx.db, task_id, idx_msg)
            idx_after_briefer[0] = get_usage_index()

        try:
            text = await asyncio.wait_for(
                _msg_task(
                    ctx.config, ctx.db, ctx.session, detail,
                    plan_outputs=ctx.plan_outputs,
                    goal=ctx.goal,
                    user_message=ctx.user_message,
                    on_briefer_done=_flush_briefer,
                ),
                timeout=ctx.messenger_timeout,
            )
        except asyncio.TimeoutError:
            raise MessengerError(f"Messenger timed out after {ctx.messenger_timeout}s")
    except (LLMError, MessengerError) as e:
        # M481: final msg task falls back to raw detail instead of killing
        # the plan. Non-final msg errors still stop execution.
        if is_final:
            log.warning("Final msg task %d messenger failed, falling back to raw detail: %s", task_id, e)
            text = detail
        else:
            task_duration_ms = int((time.perf_counter() - t0) * 1000)
            log.error("Msg task %d messenger error: %s", task_id, e)
            await update_task(ctx.db, task_id, "failed", output=str(e), duration_ms=task_duration_ms)
            _audit_task(ctx, task_id, TASK_TYPE_MSG, detail, "failed", task_duration_ms)
            # M396: append LLM calls so verbose panels show attempted messenger/briefer
            await _append_calls(ctx.db, task_id, idx_after_briefer[0])
            await _store_step_usage(ctx.db, task_id, usage_idx_before)
            return _TaskHandlerResult(stop=True, stop_success=False)

    # Shared finalization for success and is_final fallback
    task_duration_ms = int((time.perf_counter() - t0) * 1000)
    await update_task(ctx.db, task_id, "done", output=text, duration_ms=task_duration_ms)
    task_row = {**task_row, "output": text, "status": "done"}
    _audit_task(ctx, task_id, TASK_TYPE_MSG, detail, "done", task_duration_ms, len(text))
    _log_task_done(ctx, task_id, TASK_TYPE_MSG, "done", task_duration_ms)
    await _deliver_webhook_if_configured(
        ctx.db, ctx.config, ctx.session, task_id, text, is_final,
        deploy_secrets=ctx.deploy_secrets,
        session_secrets=ctx.session_secrets,
    )
    await _append_calls(ctx.db, task_id, idx_after_briefer[0])
    await _store_step_usage(ctx.db, task_id, usage_idx_before)
    return _TaskHandlerResult(
        completed_row=task_row,
        plan_output=_make_plan_output(i + 1, TASK_TYPE_MSG, detail, text, "done", session=ctx.session),
    )


# M507: shared review-result helpers (used by tool, exec, search handlers)


async def _review_stop_stuck(
    ctx: _PlanCtx, task_id: int, review: dict, plan_output: "dict | None",
    usage_idx_before: int,
) -> _TaskHandlerResult:
    """Handle REVIEW_STATUS_STUCK — shared by tool/exec/search handlers."""
    stuck_reason = review.get("reason", "")
    if ctx.slog:
        ctx.slog.info("Review → stuck: %s", stuck_reason)
    await _store_step_usage(ctx.db, task_id, usage_idx_before)
    return _TaskHandlerResult(
        stop=True, stop_success=False, stop_stuck=stuck_reason,
        plan_output=plan_output,
    )


async def _review_stop_replan(
    ctx: _PlanCtx, task_id: int, review: dict, plan_output: "dict | None",
    usage_idx_before: int, retries: int,
) -> _TaskHandlerResult:
    """Handle REVIEW_STATUS_REPLAN escalation (retries exhausted) — shared."""
    replan_reason = review.get("reason", "")
    retry_hint = review.get("retry_hint")
    if ctx.slog:
        if retries > 0:
            ctx.slog.info("Review → replan (retried %dx before escalating): %s",
                          retries, replan_reason)
        else:
            ctx.slog.info("Review → replan: %s", replan_reason)
    await _store_step_usage(ctx.db, task_id, usage_idx_before)
    if plan_output is not None and retry_hint:
        plan_output["retry_hint"] = retry_hint
    return _TaskHandlerResult(
        stop=True, stop_success=False, stop_replan=replan_reason,
        plan_output=plan_output,
    )


async def _review_finalize_ok(
    ctx: _PlanCtx, task_id: int, task_row: dict, review: dict,
    plan_output: "dict | None", usage_idx_before: int,
) -> _TaskHandlerResult:
    """Handle review ok after loop — shared by tool/exec/search handlers."""
    await _store_step_usage(ctx.db, task_id, usage_idx_before)
    if ctx.slog:
        ctx.slog.info("Review → %s", review["status"])
    if plan_output is not None and task_row.get("reviewer_summary"):
        plan_output["reviewer_summary"] = task_row["reviewer_summary"]
    if task_row["status"] == "failed":
        return _TaskHandlerResult(stop=True, stop_success=False, plan_output=plan_output)
    return _TaskHandlerResult(completed_row=task_row, plan_output=plan_output)


async def _handle_tool_task(
    ctx: _PlanCtx, task_row: dict, i: int, is_final: bool, usage_idx_before: int,
) -> _TaskHandlerResult:
    """Handle a tool task (external subprocess via tool plugin)."""
    task_id = task_row["id"]
    detail = task_row["detail"]
    tool_name = task_row.get("skill")  # DB field still "skill" until schema migration
    args_raw = task_row.get("args") or "{}"
    tool_info = ctx.installed_tools_by_name.get(tool_name)
    t0 = time.perf_counter()

    # Pre-flight: tool installed, args valid
    setup_error: str | None = None
    args: dict | None = None
    if tool_info is None:
        setup_error = f"Tool '{tool_name}' not installed"
    else:
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError as e:
            setup_error = f"Invalid tool args JSON: {e}"
        else:
            corrected = auto_correct_tool_args(args, tool_info["args_schema"])
            if corrected != args:
                log.warning("Auto-corrected tool args for task %d: %s → %s", task_id, args, corrected)
                args = corrected
            validation_errors = validate_tool_args(args, tool_info["args_schema"])
            if validation_errors:
                setup_error = "Tool args validation failed: " + "; ".join(validation_errors)

    if setup_error:
        return await _fail_task_and_audit(
            ctx, task_id, "tool", detail, setup_error, i + 1,
            replan_reason=f"Tool task failed: {setup_error}",
        )

    await _write_plan_outputs(ctx.session, ctx.plan_outputs)

    # Disk limit check (M218)
    disk_err = _check_disk_limit(ctx.config)
    if disk_err:
        return await _fail_task_and_audit(ctx, task_id, TASK_TYPE_TOOL, detail, disk_err, i + 1)

    # Snapshot workspace before execution to detect new files (M215)
    pre_snapshot = _snapshot_workspace(ctx.session)

    tool_retries = 0
    plan_output_entry: "dict | None" = None
    while True:
        await update_task_substatus(ctx.db, task_id, _SUBSTATUS_EXECUTING)
        t0 = time.perf_counter()
        stdout, stderr, success, exit_code = await _tool_task(
            ctx.session, tool_info, args, ctx.plan_outputs,
            ctx.session_secrets,
            sandbox_uid=ctx.sandbox_uid,
            max_output_size=ctx.max_output_size,
        )
        stdout, stderr = _sanitize_task_output(stdout, stderr, ctx)
        status = "done" if success else "failed"
        task_duration_ms = int((time.perf_counter() - t0) * 1000)

        # Auto-publish new files to pub/ and append URLs (M215)
        _auto_publish_skill_files(ctx.session, pre_snapshot)
        pub_urls = _report_pub_files(ctx.session, ctx.config, base_url=ctx.base_url)
        stdout += _format_pub_note(pub_urls)

        await update_task(ctx.db, task_id, status, output=stdout, stderr=stderr, duration_ms=task_duration_ms)
        task_row = {**task_row, "output": stdout, "stderr": stderr, "status": status,
                    "exit_code": exit_code}
        _audit_task(ctx, task_id, "tool", detail, task_row["status"],
                   task_duration_ms, len(task_row.get("output") or ""))
        _log_task_done(ctx, task_id, "tool", task_row["status"], task_duration_ms)

        plan_output_entry = _make_plan_output(
            i + 1, "tool", detail, task_row.get("output") or "", task_row["status"],
            session=ctx.session,
        )

        review, review_error = await _run_review_step(ctx, task_row)
        if review_error is not None:
            return await _handle_review_error(ctx, task_id, review_error, plan_output_entry, usage_idx_before)

        if review["status"] == REVIEW_STATUS_STUCK:
            return await _review_stop_stuck(ctx, task_id, review, plan_output_entry, usage_idx_before)

        if review["status"] == REVIEW_STATUS_REPLAN:
            retry_hint = review.get("retry_hint")
            # M204: internal retry for transient tool failures
            if retry_hint and tool_retries < ctx.max_worker_retries:
                tool_retries += 1
                await update_task_retry_count(ctx.db, task_id, tool_retries)
                if ctx.slog:
                    ctx.slog.info("Task %d tool retry %d/%d: %s",
                                  task_id, tool_retries, ctx.max_worker_retries, retry_hint)
                continue
            return await _review_stop_replan(ctx, task_id, review, plan_output_entry, usage_idx_before, tool_retries)

        # review ok → break out of retry loop
        break

    return await _review_finalize_ok(ctx, task_id, task_row, review, plan_output_entry, usage_idx_before)


async def _handle_exec_task(
    ctx: _PlanCtx, task_row: dict, i: int, is_final: bool, usage_idx_before: int,
) -> _TaskHandlerResult:
    """Handle an exec task (shell command via translator + executor + reviewer)."""
    task_id = task_row["id"]
    detail = task_row["detail"]
    await _write_plan_outputs(ctx.session, ctx.plan_outputs)

    # Disk limit check (M218)
    disk_err = _check_disk_limit(ctx.config)
    if disk_err:
        return await _fail_task_and_audit(ctx, task_id, TASK_TYPE_EXEC, detail, disk_err, i + 1)

    # Briefer: select relevant plan_outputs for this exec task
    idx_exec = get_usage_index()
    briefed_outputs = ctx.plan_outputs
    _exec_briefer_ok = ctx.plan_outputs and _is_briefer_budget_ok(ctx.config)
    if _exec_briefer_ok:
        try:
            pool = {"plan_outputs": _format_plan_outputs_for_msg(ctx.plan_outputs)}
            briefing = await run_briefer(
                ctx.config, "worker", detail, pool, session=ctx.session,
            )
            indices = set(briefing.get("output_indices", []))
            if indices:
                filtered = [o for o in ctx.plan_outputs if o["index"] in indices]
                if filtered:
                    briefed_outputs = filtered
        except (BrieferError, LLMError, asyncio.TimeoutError):
            log.debug("Briefer failed for worker task %d, using all plan_outputs", task_id)
    # M273: flush briefer calls so CLI renders panels before translator runs
    await _append_calls(ctx.db, task_id, idx_exec)

    # Snapshot workspace before execution to detect new files for auto-publish
    pre_snapshot = _snapshot_workspace(ctx.session)

    retry_context = ""
    exec_retries = 0
    local_plan_output: "dict | None" = None
    while True:
        await update_task_substatus(ctx.db, task_id, _SUBSTATUS_TRANSLATING)
        sys_env = get_system_env(ctx.config)
        sys_env_text = build_system_env_section(sys_env, session=ctx.session)
        _exec_outputs = briefed_outputs + ([local_plan_output] if local_plan_output else [])
        outputs_text = _format_plan_outputs_for_msg(_exec_outputs)
        idx_translate = get_usage_index()
        try:
            command = await run_exec_translator(
                ctx.config, detail, sys_env_text,
                plan_outputs_text=outputs_text, session=ctx.session,
                retry_context=retry_context,
            )
        except ExecTranslatorError as e:
            error_output = f"Translation failed: {e}"
            return await _fail_task_and_audit(
                ctx, task_id, "exec", detail, error_output, i + 1,
                output="", stderr=error_output,
            )

        await _append_calls(ctx.db, task_id, idx_translate)
        await update_task_command(ctx.db, task_id, command)
        if ctx.slog:
            ctx.slog.info("Task %d translated: %s → %s", task_id, detail[:80], command[:120])

        await update_task_substatus(ctx.db, task_id, _SUBSTATUS_EXECUTING)
        t0 = time.perf_counter()
        stdout, stderr, success, exit_code = await _exec_task(
            ctx.session, command, sandbox_uid=ctx.sandbox_uid,
            max_output_size=ctx.max_output_size,
        )
        task_duration_ms = int((time.perf_counter() - t0) * 1000)
        stdout, stderr = _sanitize_task_output(stdout, stderr, ctx)
        status = "done" if success else "failed"

        # M371: invalidate sysenv cache after package install so new binaries are visible
        if success and any(pkg_cmd in command for pkg_cmd in (
            "apt-get install", "apt install", "apk add",
            "yum install", "dnf install", "pip install",
        )):
            invalidate_cache()
            log.debug("Sysenv cache invalidated after package install: %s", command[:80])

        # Auto-publish new files to pub/ and append URLs
        _auto_publish_skill_files(ctx.session, pre_snapshot)
        pub_urls = _report_pub_files(ctx.session, ctx.config, base_url=ctx.base_url)
        stdout += _format_pub_note(pub_urls)

        await update_task(ctx.db, task_id, status, output=stdout, stderr=stderr, duration_ms=task_duration_ms)
        _audit_task(ctx, task_id, "exec", detail, status, task_duration_ms, len(stdout))
        _log_task_done(ctx, task_id, "exec", status, task_duration_ms)

        task_row = {**task_row, "output": stdout, "stderr": stderr, "status": status,
                    "exit_code": exit_code}

        local_plan_output = _make_plan_output(i + 1, "exec", detail, stdout, status, session=ctx.session)
        await _write_plan_outputs(ctx.session, ctx.plan_outputs + [local_plan_output])

        review, review_error = await _run_review_step(ctx, task_row)
        if review_error is not None:
            # M562: removed spurious update_task("failed") — task status already set by execution
            return await _handle_review_error(ctx, task_id, review_error, local_plan_output, usage_idx_before)

        if review["status"] == REVIEW_STATUS_STUCK:
            return await _review_stop_stuck(ctx, task_id, review, local_plan_output, usage_idx_before)

        if review["status"] == REVIEW_STATUS_REPLAN:
            retry_hint = review.get("retry_hint")
            if retry_hint and exec_retries < ctx.max_worker_retries:
                exec_retries += 1
                await update_task_retry_count(ctx.db, task_id, exec_retries)
                retry_context = (
                    f"Attempt {exec_retries} failed.\n"
                    f"Command: {command}\n"
                    f"Output: {stdout[:500]}\n"
                    f"Stderr: {stderr[:500]}\n"
                    f"Hint: {retry_hint}"
                )
                if ctx.slog:
                    ctx.slog.info("Task %d retry %d/%d: %s",
                                  task_id, exec_retries, ctx.max_worker_retries, retry_hint)
                continue
            return await _review_stop_replan(ctx, task_id, review, local_plan_output, usage_idx_before, exec_retries)

        # review ok → break out of retry loop
        break

    return await _review_finalize_ok(ctx, task_id, task_row, review, local_plan_output, usage_idx_before)


async def _handle_search_task(
    ctx: _PlanCtx, task_row: dict, i: int, is_final: bool, usage_idx_before: int,
) -> _TaskHandlerResult:
    """Handle a search task (searcher LLM + reviewer)."""
    task_id = task_row["id"]
    detail = task_row["detail"]
    search_retries = 0
    search_extra_context = ""
    local_plan_output: "dict | None" = None
    t0_total = time.perf_counter()

    while True:
        t0 = time.perf_counter()
        await update_task_substatus(ctx.db, task_id, _SUBSTATUS_SEARCHING)
        idx_search = get_usage_index()
        try:
            _search_outputs = ctx.plan_outputs + ([local_plan_output] if local_plan_output else [])
            outputs_text = _format_plan_outputs_for_msg(_search_outputs)
            full_context = outputs_text
            if search_extra_context:
                full_context = (full_context + "\n\n" + search_extra_context).strip()
            search_result = await _search_task(
                ctx.config, detail, task_row.get("args"),
                context=full_context,
                session=ctx.session,
                task_id=task_id,
            )
        except SearcherError as e:
            task_duration_ms = int((time.perf_counter() - t0) * 1000)
            error_output = f"Search failed: {e}"
            log.error("Search failed for task %d: %s", task_id, e)
            await update_task(ctx.db, task_id, "failed", output=error_output, duration_ms=task_duration_ms)
            _audit_task(ctx, task_id, "search", detail, "failed", task_duration_ms)
            plan_output = _make_plan_output(
                i + 1, "search", detail, error_output, "failed", session=ctx.session,
            )
            return _TaskHandlerResult(
                stop=True, stop_success=False,
                stop_replan=error_output,
                plan_output=plan_output,
            )

        # Keep local state updated; DB write deferred until reviewer approves
        task_row = {**task_row, "output": search_result, "status": "done"}
        local_plan_output = _make_plan_output(i + 1, "search", detail, search_result, "done", session=ctx.session)

        await _append_calls(ctx.db, task_id, idx_search)

        review, review_error = await _run_review_step(ctx, task_row)
        if review_error is not None:
            await update_task(ctx.db, task_id, "done", output=search_result)
            return await _handle_review_error(ctx, task_id, review_error, local_plan_output, usage_idx_before)

        if review["status"] == REVIEW_STATUS_STUCK:
            await update_task(ctx.db, task_id, "done", output=search_result)
            return await _review_stop_stuck(ctx, task_id, review, local_plan_output, usage_idx_before)

        if review["status"] == REVIEW_STATUS_REPLAN:
            retry_hint = review.get("retry_hint")
            if retry_hint and search_retries < ctx.max_worker_retries:
                search_retries += 1
                await update_task_retry_count(ctx.db, task_id, search_retries)
                search_extra_context += (
                    f"\n\n[Retry {search_retries}] Previous search was insufficient. "
                    f"Hint: {retry_hint}"
                )
                if ctx.slog:
                    ctx.slog.info("Task %d search retry %d/%d: %s",
                                  task_id, search_retries, ctx.max_worker_retries, retry_hint)
                continue
            await update_task(ctx.db, task_id, "done", output=search_result)
            return await _review_stop_replan(ctx, task_id, review, local_plan_output, usage_idx_before, search_retries)

        # review ok → write final status and break out of retry loop
        break

    task_duration_ms = int((time.perf_counter() - t0_total) * 1000)
    await update_task(ctx.db, task_id, "done", output=search_result, duration_ms=task_duration_ms)
    _audit_task(ctx, task_id, "search", detail, "done", task_duration_ms, len(search_result))
    _log_task_done(ctx, task_id, "search", "done", task_duration_ms)
    return await _review_finalize_ok(ctx, task_id, task_row, review, local_plan_output, usage_idx_before)


_TASK_HANDLERS: dict = {
    TASK_TYPE_EXEC: _handle_exec_task,
    TASK_TYPE_MSG: _handle_msg_task,
    TASK_TYPE_TOOL: _handle_tool_task,
    TASK_TYPE_SEARCH: _handle_search_task,
    TASK_TYPE_REPLAN: _handle_replan_task,
}


async def _execute_plan(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int,
    goal: str,
    user_message: str,
    messenger_timeout: int = 120,
    session_secrets: dict[str, str] | None = None,
    username: str | None = None,
    cancel_event: asyncio.Event | None = None,
    slog: SessionLogger | None = None,
    base_url: str = "",
) -> tuple[bool, str | None, str | None, list[dict], list[dict], list[dict]]:
    """Execute a plan's tasks. Returns (success, replan_reason, stuck_reason, completed, remaining, plan_outputs).

    - success: True if all tasks completed successfully
    - replan_reason: reviewer reason if replan needed, None otherwise
    - completed: list of completed task dicts (with outputs)
    - remaining: list of unexecuted task dicts
    - plan_outputs: list of plan output dicts (may contain retry_hint)
    """
    tasks = await get_tasks_for_plan(db, plan_id)
    completed: list[dict] = []
    deploy_secrets = collect_deploy_secrets()
    max_output_size = setting_int(config.settings, "max_output_size", lo=0)
    max_worker_retries = setting_int(config.settings, "max_worker_retries", lo=0)
    # Cache installed tools for the whole plan execution (avoid rescanning per task)
    installed_tools = discover_tools()

    ctx = _PlanCtx(
        db=db,
        config=config,
        session=session,
        goal=goal,
        user_message=user_message,
        deploy_secrets=deploy_secrets,
        session_secrets=session_secrets or {},
        max_output_size=max_output_size,
        max_worker_retries=max_worker_retries,
        messenger_timeout=messenger_timeout,
        installed_tools=installed_tools,
        slog=slog,
        base_url=base_url,
        sandbox_uid=None,
    )

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
            await _cleanup_plan_outputs(session)
            return False, "cancelled", None, completed, [dict(t) for t in tasks[i:]], ctx.plan_outputs

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
            tool_name=task_row.get("skill"),
        )
        if not perm.allowed:
            await update_task(db, task_id, "failed", output=perm.reason)
            audit.log_task(
                session, task_id, task_type, detail, "failed", 0, 0,
                deploy_secrets=deploy_secrets,
                session_secrets=session_secrets or {},
            )
            remaining = [dict(t) for t in tasks[i + 1:]]
            await _cleanup_plan_outputs(session)
            return False, None, None, completed, remaining, ctx.plan_outputs

        sandbox_uid = await _ensure_sandbox_user(session) if perm.role == "user" else None
        if sandbox_uid is not None:
            _session_workspace(session, sandbox_uid=sandbox_uid)
        ctx.sandbox_uid = sandbox_uid

        await update_task(db, task_id, "running")
        if slog:
            slog.info("Task %d started: [%s] %s", task_id, task_type, detail[:120])

        usage_idx_before = get_usage_index()

        # --- Dispatch to handler ---
        handler = _TASK_HANDLERS.get(task_type)
        if handler is None:
            log.error("Unknown task type %r for task %d", task_type, task_id)
            await update_task(db, task_id, "failed", output=f"Unknown task type: {task_type}")
            remaining = [dict(t) for t in tasks[i + 1:]]
            await _cleanup_plan_outputs(session)
            return False, None, None, completed, remaining, ctx.plan_outputs

        is_final = i == len(tasks) - 1
        result = await handler(ctx, task_row, i, is_final, usage_idx_before)

        # Refresh tool cache after exec/tool tasks (may have installed new tools)
        if task_type in (TASK_TYPE_EXEC, TASK_TYPE_TOOL):
            invalidate_tools_cache()
            ctx.installed_tools = discover_tools()
            # After an install exec, unhealthy tools may need a moment for
            # binary downloads to finish (e.g. Chromium for browser tool).
            # Rescan once after a short delay to avoid false install-loop.
            if (
                task_type == TASK_TYPE_EXEC
                and not result.stop
                and _INSTALL_CMD_RE.search(detail or "")
                and any(not t.get("healthy", True) for t in ctx.installed_tools)
            ):
                await asyncio.sleep(_POST_INSTALL_RESCAN_DELAY)
                invalidate_tools_cache()
                ctx.installed_tools = discover_tools()
                still_unhealthy = [t["name"] for t in ctx.installed_tools if not t.get("healthy", True)]
                log.info(
                    "Post-install rescan complete: %d tools, %d still unhealthy%s",
                    len(ctx.installed_tools),
                    len(still_unhealthy),
                    f" ({', '.join(still_unhealthy)})" if still_unhealthy else "",
                )
            ctx.installed_tools_by_name = {s["name"]: s for s in ctx.installed_tools}

        if result.plan_output is not None:
            ctx.plan_outputs.append(result.plan_output)
        if result.completed_row is not None:
            completed.append(result.completed_row)

        if result.stop:
            remaining = [dict(t) for t in tasks[i + 1:]]
            await _cleanup_plan_outputs(session)
            return result.stop_success, result.stop_replan, result.stop_stuck, completed, remaining, ctx.plan_outputs

    await _cleanup_plan_outputs(session)
    return True, None, None, completed, [], ctx.plan_outputs


async def _apply_curator_result(
    db: aiosqlite.Connection, session: str, result: dict
) -> None:
    """Apply curator evaluations: promote facts, create pending questions, discard."""
    for ev in result.get("evaluations", []):
        lid = ev.get("learning_id")
        verdict = ev.get("verdict")
        if lid is None or verdict is None:
            log.warning("Curator evaluation missing learning_id or verdict, skipping: %s", ev)
            continue
        if verdict == CURATOR_VERDICT_PROMOTE:
            fact_content = ev.get("fact")
            if not fact_content:
                log.warning("Curator promote verdict has no fact content for learning_id=%s", lid)
                await update_learning(db, lid, "discarded")
                continue
            category = ev.get("category") or "general"
            fact_session = session if category == "user" else None
            tags = ev.get("tags") or None
            entity_id = None
            if ev.get("entity_name") and ev.get("entity_kind"):
                entity_id = await find_or_create_entity(db, ev["entity_name"], ev["entity_kind"])
            await save_fact(
                db, fact_content, source="curator",
                session=fact_session, category=category, tags=tags,
                entity_id=entity_id,
            )
            await update_learning(db, lid, "promoted")
        elif verdict == CURATOR_VERDICT_ASK:
            question = ev.get("question")
            if not question:
                log.warning("Curator ask verdict has no question for learning_id=%s", lid)
                await update_learning(db, lid, "discarded")
                continue
            await save_pending_item(db, question, scope=session, source="curator")
            await update_learning(db, lid, "promoted")
        elif verdict == CURATOR_VERDICT_DISCARD:
            await update_learning(db, lid, "discarded")


async def run_worker(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    queue: asyncio.Queue,
    cancel_event: asyncio.Event | None = None,
    set_phase: Callable[[str], None] | None = None,
    pending_messages: list | None = None,
    update_hints: list | None = None,
):
    """Worker loop for a session. Drains queue, plans, executes tasks."""
    idle_timeout = setting_float(config.settings, "worker_idle_timeout", lo=0.01)
    classifier_timeout = setting_int(config.settings, "classifier_timeout", lo=1)
    llm_timeout = setting_int(config.settings, "llm_timeout", lo=1)
    messenger_timeout = llm_timeout  # unified (M422)
    max_replan_depth = setting_int(config.settings, "max_replan_depth", lo=0)
    slog = SessionLogger(session, base_dir=KISO_DIR)

    _pending_knowledge_task: asyncio.Task | None = None

    try:
        while True:
            _notify_phase(set_phase, WORKER_PHASE_IDLE)

            # M408: drain pending_messages (independent in-flight messages)
            # into the queue before waiting for new messages
            if pending_messages:
                while pending_messages:
                    pm = pending_messages.pop(0)
                    try:
                        queue.put_nowait(pm)
                    except asyncio.QueueFull:
                        log.warning("Queue full while draining pending message %d", pm.get("id", 0))
                        break

            try:
                msg = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                log.info("Worker idle timeout for session=%s, shutting down", session)
                slog.info("Worker idle — shutting down")
                break

            # Await previous background knowledge task before processing next msg
            if _pending_knowledge_task is not None:
                try:
                    await _pending_knowledge_task
                except Exception:
                    log.exception("Background knowledge task failed for session=%s", session)
                _pending_knowledge_task = None

            try:
                _pending_knowledge_task = await _process_message(
                    db, config, session, msg, cancel_event,
                    llm_timeout,
                    max_replan_depth, classifier_timeout=classifier_timeout,
                    messenger_timeout=messenger_timeout,
                    slog=slog, set_phase=set_phase,
                    update_hints=update_hints)
            except Exception:
                log.exception("Unexpected error processing message in session=%s", session)
                slog.error("Unexpected error processing message")
                continue
    finally:
        # Await pending background knowledge task on shutdown
        if _pending_knowledge_task is not None:
            try:
                await _pending_knowledge_task
            except Exception:
                log.exception("Background knowledge task failed during shutdown for session=%s", session)
        _notify_phase(set_phase, WORKER_PHASE_IDLE)
        slog.close()


async def _bump_fact_usage(
    db: aiosqlite.Connection,
    content: str,
    session: str,
    user_role: str,
) -> None:
    """Search for facts relevant to *content* and increment their use counters."""
    used_facts = await search_facts(
        db, content, session=session, is_admin=user_role == "admin",
    )
    if used_facts:
        await update_fact_usage(db, [f["id"] for f in used_facts])


# ---------------------------------------------------------------------------
# M91b: Planning-loop termination handlers
# ---------------------------------------------------------------------------


async def _handle_loop_success(
    db: aiosqlite.Connection,
    session: str,
    plan_id: int,
    content: str,
    user_role: str,
    slog: "SessionLogger | None",
) -> None:
    """Mark plan done and bump fact usage on successful completion."""
    await update_plan_status(db, plan_id, "done")
    log.info("Plan %d done", plan_id)
    if slog:
        slog.info("Plan %d done", plan_id)
    await _bump_fact_usage(db, content, session, user_role)


async def _msg_task_with_fallback(
    config: Config,
    db: aiosqlite.Connection,
    session: str,
    detail: str,
    goal: str,
    timeout: "int | float",
    user_message: str = "",
) -> str:
    """Call _msg_task with a timeout; fall back to raw detail on any error."""
    try:
        return await asyncio.wait_for(
            _msg_task(config, db, session, detail, goal=goal,
                      user_message=user_message),
            timeout=timeout,
        )
    except (LLMError, MessengerError, asyncio.TimeoutError):
        return detail


async def _handle_loop_cancel(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int,
    completed: list[dict],
    remaining: list[dict],
    goal: str,
    *,
    messenger_timeout: "int | float" = 120,
    session_secrets: dict | None = None,
    cancel_event: "asyncio.Event | None" = None,
    user_message: str = "",
) -> None:
    """Mark plan cancelled, send cancel summary, clear cancel event.

    M307: create the msg task BEFORE changing plan status so the CLI's
    stability window doesn't expire while the messenger is composing.
    """
    cancel_detail = _build_cancel_summary(completed, remaining, goal)
    # Create placeholder task first — CLI will see it and wait for it to finish
    cancel_task_id = await create_task(db, plan_id, session, TASK_TYPE_MSG, cancel_detail)
    await update_task(db, cancel_task_id, "running")
    cancel_text = await _msg_task_with_fallback(
        config, db, session, cancel_detail, goal, messenger_timeout,
        user_message=user_message,
    )
    await update_task(db, cancel_task_id, status="done", output=cancel_text)
    # Update plan usage before status change so CLI shows accurate totals
    usage = get_usage_summary()
    if usage["input_tokens"] or usage["output_tokens"]:
        await update_plan_usage(
            db, plan_id, usage["input_tokens"], usage["output_tokens"], usage["model"],
        )
    await update_plan_status(db, plan_id, "cancelled")
    await save_message(db, session, None, "system", cancel_text, trusted=True, processed=True)
    await _deliver_webhook_if_configured(
        db, config, session, 0, cancel_text, True,
        deploy_secrets=collect_deploy_secrets(),
        session_secrets=session_secrets or {},
    )
    if cancel_event is not None:
        cancel_event.clear()


async def _handle_loop_failure(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int,
    completed: list[dict],
    remaining: list[dict],
    goal: str,
    *,
    messenger_timeout: "int | float" = 120,
    reason: str | None = None,
    session_secrets: dict | None = None,
    deliver_webhook: bool = True,
    user_message: str = "",
) -> None:
    """Mark plan failed, send failure message, optionally deliver webhook.

    M307: create the msg task BEFORE changing plan status so the CLI's
    stability window doesn't expire while the messenger is composing.
    """
    fail_detail = _build_failure_summary(completed, remaining, goal, reason=reason)
    # Create placeholder task first — CLI will see it and wait for it to finish
    fail_task_id = await create_task(db, plan_id, session, TASK_TYPE_MSG, fail_detail)
    await update_task(db, fail_task_id, "running")
    fail_text = await _msg_task_with_fallback(
        config, db, session, fail_detail, goal, messenger_timeout,
        user_message=user_message,
    )
    await update_task(db, fail_task_id, status="done", output=fail_text)
    # Update plan usage before status change so CLI shows accurate totals
    usage = get_usage_summary()
    if usage["input_tokens"] or usage["output_tokens"]:
        await update_plan_usage(
            db, plan_id, usage["input_tokens"], usage["output_tokens"], usage["model"],
        )
    await update_plan_status(db, plan_id, "failed")
    await save_message(db, session, None, "system", fail_text, trusted=True, processed=True)
    if deliver_webhook:
        await _deliver_webhook_if_configured(
            db, config, session, 0, fail_text, True,
            deploy_secrets=collect_deploy_secrets(),
            session_secrets=session_secrets or {},
        )


# ---------------------------------------------------------------------------
# M62c: Extracted planning loop
# ---------------------------------------------------------------------------

async def _run_planning_loop(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    msg_id: int,
    content: str,
    plan_id: int,
    plan: dict,
    user_role: str,
    user_tools: "str | list[str] | None",
    messenger_timeout: int,
    session_secrets: dict,
    cancel_event: "asyncio.Event | None",
    max_replan_depth: int,
    username: "str | None",
    slog: "SessionLogger | None",
    set_phase: "Callable[[str], None] | None" = None,
    base_url: str = "",
    update_hints: "list | None" = None,
) -> int:
    """Execute plan with replan loop. Returns the final plan_id."""

    replan_history: list[dict] = []
    current_plan_id = plan_id
    current_goal = plan["goal"]
    replan_depth = 0
    total_extensions = 0

    while True:
        success, replan_reason, stuck_reason, completed, remaining, plan_outputs = await _execute_plan(
            db, config, session, current_plan_id, current_goal,
            content, messenger_timeout=messenger_timeout,
            session_secrets=session_secrets, username=username,
            cancel_event=cancel_event, slog=slog, base_url=base_url,
        )

        if success:
            await _handle_loop_success(db, session, current_plan_id, content, user_role, slog)
            break

        # --- Stuck handling (M336): unsolvable task, skip replan entirely ---
        if stuck_reason:
            log.info("Task stuck (unsolvable): %s", stuck_reason)
            if slog:
                slog.info("Task stuck (unsolvable): %s", stuck_reason)
            await _handle_loop_failure(
                db, config, session, current_plan_id, completed, remaining, current_goal,
                messenger_timeout=messenger_timeout,
                reason=f"Unsolvable: {stuck_reason}",
                session_secrets=session_secrets,
                user_message=content,
            )
            break

        # --- Cancel handling ---
        if replan_reason == "cancelled":
            await _handle_loop_cancel(
                db, config, session, current_plan_id, completed, remaining, current_goal,
                messenger_timeout=messenger_timeout,
                session_secrets=session_secrets, cancel_event=cancel_event,
                user_message=content,
            )
            break

        if replan_reason is None:
            # Auto-replan safety net (M172): if there are failed outputs and
            # replan budget remains, generate a reason from the last failure.
            failed_outputs = [
                po for po in plan_outputs if po.get("status") == "failed"
            ]
            if failed_outputs and replan_depth < max_replan_depth:
                last_fail = failed_outputs[-1]
                replan_reason = f"Task failed: {(last_fail.get('output') or 'unknown error')[:200]}"
                log.info("Auto-generating replan reason (M172): %s", replan_reason)
            else:
                log.info("Plan %d failed (no replan)", current_plan_id)
                if slog:
                    slog.info("Plan %d failed", current_plan_id)
                await _handle_loop_failure(
                    db, config, session, current_plan_id, completed, remaining, current_goal,
                    messenger_timeout=messenger_timeout,
                    session_secrets=session_secrets,
                    user_message=content,
                )
                break

        # Replan requested
        replan_depth += 1
        if replan_depth > max_replan_depth:
            remaining_tasks = await get_tasks_for_plan(db, current_plan_id)
            for t in remaining_tasks:
                if t["status"] == "pending":
                    await update_task(db, t["id"], "failed", output="Max replan depth reached")
            log.warning("Max replan depth (%d) reached for session=%s", max_replan_depth, session)
            await _handle_loop_failure(
                db, config, session, current_plan_id, completed, remaining, current_goal,
                messenger_timeout=messenger_timeout,
                reason=f"Max replan depth ({max_replan_depth}) reached. "
                       f"Last failure: {replan_reason}",
                session_secrets=session_secrets,
                user_message=content,
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
                await update_task(db, t["id"], "skipped", output="skipped — superseded by replan")

        # Build replan history
        tried = [f"[{t['type']}] {t['detail']}" for t in completed]
        key_outputs = []
        for t in completed:
            out = (t.get("output") or "")[:500]
            if out:
                key_outputs.append(f"[{t['type']}] {out}")
        # Extract retry hints and reviewer summaries from plan_outputs (M145/M550)
        retry_hints = [
            po["retry_hint"] for po in plan_outputs
            if po.get("retry_hint")
        ]
        reviewer_summaries = [
            po["summary"] for po in plan_outputs
            if po.get("summary")
        ]
        # Strategy fingerprint: sorted set of "type:detail_prefix" for each task
        strategy_fp = frozenset(
            f"{t['type']}:{t['detail'][:30]}" for t in completed
        )
        history_entry: dict = {
            "goal": current_goal,
            "failure": replan_reason,
            "what_was_tried": tried,
            "key_outputs": key_outputs,
            "strategy_fingerprint": strategy_fp,
        }
        if retry_hints:
            history_entry["retry_hints"] = retry_hints
        if reviewer_summaries:
            history_entry["reviewer_summaries"] = reviewer_summaries
        replan_history.append(history_entry)

        # Detect circular replanning (M337: scan ALL history, not just [-2] vs [-1])
        # 1. Word overlap in failure reasons (>60%) against any previous entry
        # 2. Strategy fingerprint similarity (>50% Jaccard) against any previous entry
        stuck_detected = False
        if len(replan_history) >= 2:
            curr_words = set(replan_history[-1]["failure"].lower().split())
            curr_fp = replan_history[-1].get("strategy_fingerprint", frozenset())
            for prev in replan_history[:-1]:
                if stuck_detected:
                    break
                # Word overlap check
                prev_words = set(prev["failure"].lower().split())
                if prev_words and curr_words:
                    ratio = len(prev_words & curr_words) / max(len(prev_words), len(curr_words))
                    if ratio > 0.6:
                        stuck_detected = True
                        log.warning("Circular replan detected (%.0f%% failure word overlap): %s",
                                    ratio * 100, replan_reason)
                        break
                # Strategy fingerprint check
                prev_fp = prev.get("strategy_fingerprint", frozenset())
                if prev_fp and curr_fp:
                    union = prev_fp | curr_fp
                    jaccard = len(prev_fp & curr_fp) / len(union) if union else 0
                    if jaccard > 0.5:
                        stuck_detected = True
                        log.warning("Circular replan detected (%.0f%% strategy overlap): %s",
                                    jaccard * 100, replan_reason)

        # M371: detect install→use→fail loops
        if not stuck_detected and len(replan_history) >= 2:
            _install_kws = {"install", "apt-get", "apt", "apk", "yum", "dnf", "pip"}
            _fail_kws = {"not found", "cannot_translate", "command not found",
                         "no such file", "not installed"}
            has_install = any(
                _install_kws & set(h["failure"].lower().split())
                for h in replan_history[:-1]
            )
            curr_fail_lower = replan_history[-1]["failure"].lower()
            has_not_found = any(kw in curr_fail_lower for kw in _fail_kws)
            if has_install and has_not_found:
                stuck_detected = True
                log.warning("Install loop detected: installed package but binary still not found")

        # Notify user about replan (as a visible msg task + webhook)
        user_lang = detect_user_lang(content)
        if stuck_detected:
            tried_summary = "; ".join(
                f"{h['goal']}: {h['failure']}" for h in replan_history[-2:]
            )
            msg_text = get_replan_message(
                user_lang, "stuck", replan_depth, max_replan_depth,
                reason=replan_reason, tried=tried_summary,
            )
        elif is_self_directed:
            msg_text = get_replan_message(
                user_lang, "investigating", replan_depth, max_replan_depth,
            )
        else:
            msg_text = get_replan_message(
                user_lang, "replanning", replan_depth, max_replan_depth,
                reason=replan_reason,
            )
        replan_notify_id = await create_task(db, current_plan_id, session, TASK_TYPE_MSG, msg_text)
        await update_task(db, replan_notify_id, status="done", output=msg_text)
        await save_message(db, session, None, "system", msg_text, trusted=True, processed=True)
        await _deliver_webhook_if_configured(
            db, config, session, replan_notify_id, msg_text, False,
            deploy_secrets=collect_deploy_secrets(),
            session_secrets=session_secrets,
        )

        # M429: stuck_detected → stop replan loop immediately
        if stuck_detected:
            log.info("Stuck detected — stopping replan loop session=%s", session)
            await _handle_loop_failure(
                db, config, session, current_plan_id, completed, remaining,
                current_goal,
                messenger_timeout=messenger_timeout,
                reason=f"Circular replanning detected: {replan_reason}",
                deliver_webhook=False,  # already sent above
                session_secrets=session_secrets,
                user_message=content,
            )
            break

        # --- Cancel check before replan ---
        if cancel_event is not None and cancel_event.is_set():
            await _handle_loop_cancel(
                db, config, session, current_plan_id, completed, remaining, current_goal,
                messenger_timeout=messenger_timeout,
                session_secrets=session_secrets, cancel_event=cancel_event,
                user_message=content,
            )
            break

        # Call planner with enriched context
        _notify_phase(set_phase, WORKER_PHASE_PLANNING)
        # M409: drain update hints into replan context, then clear them
        consumed_hints = list(update_hints) if update_hints else None
        if update_hints:
            update_hints.clear()
        replan_context = _build_replan_context(
            completed, remaining, replan_reason, replan_history,
            update_hints=consumed_hints,
        )
        enriched_message = f"{content}\n\n{replan_context}"

        def _on_replan_retry(attempt: int, max_attempts: int, reason: str) -> None:
            log.info("Retrying replan (attempt %d/%d) session=%s: %s",
                     attempt, max_attempts, session, reason)

        replan_usage_idx = get_usage_index()
        try:
            new_plan = await run_planner(
                db, config, session, user_role, enriched_message,
                user_tools=user_tools,
                on_retry=_on_replan_retry,
                is_replan=True,
            )
        except PlanError as e:
            log.error("Replan failed: %s", e)
            await _handle_loop_failure(
                db, config, session, current_plan_id, completed, remaining, current_goal,
                messenger_timeout=messenger_timeout,
                reason=f"Replan failed: {e}",
                deliver_webhook=False,
                user_message=content,
            )
            break

        # Create new plan BEFORE finalizing the old one, so the CLI never
        # sees a window where the old plan is done/failed but no successor
        # exists yet (M103a — race condition fix).
        new_plan_id = await create_plan(
            db, session, msg_id, new_plan["goal"],
            parent_id=current_plan_id,
        )
        # M279: skip intent injection on replan — user already saw the intent
        await _persist_plan_tasks(db, new_plan_id, session, new_plan["tasks"])

        # Now finalize old plan status — the new plan is already visible.
        if is_self_directed:
            await update_plan_status(db, current_plan_id, "done")
        else:
            await update_plan_status(db, current_plan_id, "failed")
        log.info("Replan %d (parent=%d): goal=%r, %d tasks",
                 new_plan_id, current_plan_id, new_plan["goal"], len(new_plan["tasks"]))
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

        # Handle extend_replan: planner can request extra attempts, capped globally
        # M338: deny extensions when stuck pattern detected
        extend = new_plan.get("extend_replan")
        if extend and isinstance(extend, int) and extend > 0:
            remaining_budget = _MAX_EXTEND_REPLAN - total_extensions
            if remaining_budget <= 0:
                log.info("Planner requested extend_replan=%d but global cap (%d) reached",
                         extend, _MAX_EXTEND_REPLAN)
            else:
                extend = min(extend, remaining_budget)
                total_extensions += extend
                max_replan_depth += extend
                log.info("Planner granted %d extra replan attempts (total extensions: %d/%d, new limit: %d)",
                         extend, total_extensions, _MAX_EXTEND_REPLAN, max_replan_depth)

        current_plan_id = new_plan_id
        current_goal = new_plan["goal"]
        _notify_phase(set_phase, WORKER_PHASE_EXECUTING)

    return current_plan_id


async def _handle_plan_error(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    msg_id: int,
    error_text: str,
    plan_id: int | None = None,
) -> None:
    """Persist a planning failure: create failed plan+task, save message, deliver webhook."""
    if plan_id is None:
        fail_plan_id = await create_plan(db, session, msg_id, "Failed")
    else:
        fail_plan_id = plan_id
        await update_plan_goal(db, fail_plan_id, "Failed")
    fail_task_id = await create_task(db, fail_plan_id, session, TASK_TYPE_MSG, error_text)
    await update_task(db, fail_task_id, status="done", output=error_text)
    await update_plan_status(db, fail_plan_id, "failed")
    await save_message(db, session, None, "system", error_text, trusted=True, processed=True)
    await _deliver_webhook_if_configured(
        db, config, session, 0, error_text, True,
        deploy_secrets=collect_deploy_secrets(),
    )


async def _process_message(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    msg: dict,
    cancel_event: asyncio.Event | None,
    llm_timeout: int,
    max_replan_depth: int,
    classifier_timeout: int = 30,
    messenger_timeout: int = 120,
    slog: SessionLogger | None = None,
    set_phase: Callable[[str], None] | None = None,
    update_hints: list | None = None,
) -> asyncio.Task | None:
    """Process a single message. Returns a background knowledge task (or None)."""
    msg_id: int = msg["id"]
    content: str = msg["content"]
    user_role: str = msg["user_role"]
    user_tools: str | list[str] | None = msg.get("user_tools")
    username: str | None = msg.get("username")
    base_url: str = msg.get("base_url", "")

    if slog:
        slog.info("Message received: user=%s, %d chars", username or "?", len(content))

    # Per-message LLM call budget and usage tracking
    max_llm_calls = setting_int(config.settings, "max_llm_calls_per_message", lo=1)
    set_llm_budget(max_llm_calls)
    reset_usage_tracking()

    await mark_message_processed(db, msg_id)

    # --- Fast path: skip planner for conversational messages ---
    # Paraphraser is intentionally skipped here — the messenger only sees
    # session summary + facts + the current user message (all trusted).
    # Untrusted messages feed into planner context, not messenger context.
    # Fetch last plan goal for classifier context (M276) before creating new plan
    _prev_plan = await get_plan_for_session(db, session)
    _classifier_ctx = ""
    if _prev_plan and _prev_plan.get("goal") and _prev_plan["goal"] != "Planning...":
        goal = _prev_plan["goal"]
        _classifier_ctx = f"Last plan goal: {goal[:150]}"

    # Create plan record before classifier so the CLI can render it immediately.
    # This ensures the plan header appears before inflight indicators.
    plan_id = await create_plan(db, session, msg_id, "Planning...")

    fast_path_enabled = setting_bool(config.settings, "fast_path_enabled")
    user_lang = "en"  # default; overwritten by classifier
    if fast_path_enabled:
        _notify_phase(set_phase, WORKER_PHASE_CLASSIFYING)
        try:
            msg_class, user_lang = await asyncio.wait_for(
                classify_message(
                    config, content, session=session,
                    recent_context=_classifier_ctx,
                ),
                timeout=classifier_timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Classifier timed out after %ds, falling back to planner",
                        classifier_timeout)
            msg_class = "plan"
        if msg_class in ("chat", "chat_kb"):
            log.info("Fast path: %s message, skipping planner", msg_class)
            if slog:
                slog.info("Fast path: classified as %s, skipping planner", msg_class)
            _notify_phase(set_phase, WORKER_PHASE_EXECUTING)
            fast_plan_id = await _fast_path_chat(
                db, config, session, msg_id, content,
                messenger_timeout=messenger_timeout, slog=slog,
                plan_id=plan_id, user_lang=user_lang,
            )
            # Bump fact usage for fast path (facts contributed to chat response)
            await _bump_fact_usage(db, content, session, user_role)
            # Spawn post-plan knowledge processing in background
            clear_llm_budget()
            _notify_phase(set_phase, WORKER_PHASE_IDLE)
            return _spawn_knowledge_task(db, config, session, fast_plan_id, llm_timeout)

    # Store classifier usage immediately so verbose panels can render
    classifier_usage = get_usage_since(0)
    if classifier_usage["input_tokens"] or classifier_usage["output_tokens"]:
        await update_plan_usage(
            db, plan_id,
            classifier_usage["input_tokens"], classifier_usage["output_tokens"],
            classifier_usage["model"],
            llm_calls=classifier_usage.get("calls"),
        )

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
    _notify_phase(set_phase, WORKER_PHASE_PLANNING)
    planner_usage_idx = get_usage_index()

    async def _flush_pre_planner_usage():
        """Store briefer/paraphraser usage so CLI can render panels while planner runs."""
        usage = get_usage_since(0)
        if usage["input_tokens"] or usage["output_tokens"]:
            await update_plan_usage(
                db, plan_id,
                usage["input_tokens"], usage["output_tokens"],
                usage["model"], llm_calls=usage.get("calls"),
            )

    def _on_planner_retry(attempt: int, max_attempts: int, reason: str) -> None:
        log.info("Retrying planner (attempt %d/%d) session=%s: %s",
                 attempt, max_attempts, session, reason)

    # M428: check if user approved install in a prior msg cycle
    _install_approved = await session_has_install_proposal(db, session)

    try:
        plan = await run_planner(
            db, config, session, user_role, content,
            user_tools=user_tools,
            paraphrased_context=paraphrased_context,
            on_context_ready=_flush_pre_planner_usage,
            on_retry=_on_planner_retry,
            install_approved=_install_approved,
        )
    except PlanError as e:
        log.error("Planning failed session=%s msg=%d: %s", session, msg_id, e)
        await _handle_plan_error(db, config, session, msg_id, f"Planning failed: {e}",
                                 plan_id=plan_id)
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
    deploy_secrets = collect_deploy_secrets()
    for t in plan["tasks"]:
        t["detail"] = sanitize_output(t["detail"], deploy_secrets, session_secrets)
        if t.get("args"):
            t["args"] = sanitize_output(t["args"], deploy_secrets, session_secrets)

    # Update plan with real goal and persist tasks
    await update_plan_goal(db, plan_id, plan["goal"])

    plan_tasks = _maybe_inject_intent_msg(plan["tasks"], plan["goal"])

    await _persist_plan_tasks(db, plan_id, session, plan_tasks)
    log.info("Plan %d: goal=%r, %d tasks", plan_id, plan["goal"], len(plan_tasks))
    if slog:
        slog.info("Plan %d created: %s (%d tasks)", plan_id, plan["goal"], len(plan_tasks))

    # Store planner usage (incremental from planner_usage_idx) merged with classifier
    all_usage = get_usage_since(0)
    if all_usage["input_tokens"] or all_usage["output_tokens"]:
        await update_plan_usage(
            db, plan_id,
            all_usage["input_tokens"], all_usage["output_tokens"],
            all_usage["model"],
            llm_calls=all_usage.get("calls"),
        )

    # Execute with replan loop
    _notify_phase(set_phase, WORKER_PHASE_EXECUTING)
    current_plan_id = await _run_planning_loop(
        db, config, session, msg_id, content,
        plan_id, plan, user_role, user_tools, messenger_timeout,
        session_secrets, cancel_event, max_replan_depth,
        username, slog, set_phase=set_phase, base_url=base_url,
        update_hints=update_hints,
    )

    # --- Store token usage on the final plan ---
    # Only update totals; llm_calls is preserved (planner-only, set earlier).
    usage = get_usage_summary()
    if current_plan_id and (usage["input_tokens"] or usage["output_tokens"]):
        await update_plan_usage(
            db, current_plan_id,
            usage["input_tokens"], usage["output_tokens"], usage["model"],
        )

    # --- Invalidate system env cache (exec tasks may have changed the system) ---
    invalidate_cache()

    # --- Spawn post-plan knowledge processing in background ---
    clear_llm_budget()
    _notify_phase(set_phase, WORKER_PHASE_IDLE)
    return _spawn_knowledge_task(db, config, session, current_plan_id, llm_timeout)
