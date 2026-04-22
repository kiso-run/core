"""Message-oriented worker support helpers extracted from the main loop."""

from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

from kiso import audit
from kiso.brain import (
    _MAX_MESSENGER_FACTS,
    BrieferError,
    ConsolidatorError,
    CuratorError,
    SummarizerError,
    _build_worker_memory_pack,
    apply_consolidation_result,
    run_briefer,
    run_consolidator,
    run_curator,
    run_messenger,
    run_summarizer,
)
from kiso.config import Config, setting_bool, setting_float, setting_int
from kiso.llm import (
    clear_llm_budget,
    get_usage_since,
    get_usage_summary,
    LLMError,
    reset_usage_tracking,
    set_llm_budget,
)
from kiso.store import (
    _normalize_entity_name,
    append_task_llm_call,
    archive_low_confidence_facts,
    backfill_fact_entities,
    count_messages,
    decay_facts,
    get_all_entities,
    get_all_tags,
    get_facts,
    get_kv,
    get_session_project_id,
    get_oldest_messages,
    get_pending_learnings,
    get_session,
    search_facts_scored,
    set_kv,
    update_plan_usage,
    update_summary,
)
from kiso.webhook import deliver_webhook
from kiso.worker.utils import _format_plan_outputs_for_msg

log = logging.getLogger(__name__)

_BRIEFER_MSG_TIMEOUT: float = 30.0
_LAST_CONSOLIDATION_KV_KEY = "last_consolidation_time"


async def _append_calls_impl(
    db: aiosqlite.Connection, task_id: int, idx_before: int
) -> None:
    """Append individual LLM call entries (since idx_before) to the task row."""
    try:
        usage = get_usage_since(idx_before)
        for call in usage.get("calls") or []:
            await append_task_llm_call(db, task_id, call)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_append_calls: failed to store LLM calls for task %d: %s", task_id, exc
        )


async def _deliver_webhook_if_configured_impl(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    task_id: int,
    content: str,
    final: bool,
    deploy_secrets: dict[str, str] | None = None,
    session_secrets: dict[str, str] | None = None,
    deliver_webhook_fn=deliver_webhook,
    audit_mod=audit,
) -> None:
    """Deliver a webhook if the session has one configured. No-op otherwise."""
    sess = await get_session(db, session)
    webhook_url = sess.get("webhook") if sess else None
    if not webhook_url:
        return
    wh_success, wh_status, wh_attempts = await deliver_webhook_fn(
        webhook_url,
        session,
        task_id,
        content,
        final,
        secret=str(config.settings["webhook_secret"]),
        max_payload=setting_int(config.settings, "webhook_max_payload", lo=1),
    )
    audit_mod.log_webhook(
        session,
        task_id,
        webhook_url,
        wh_status,
        wh_attempts,
        deploy_secrets=deploy_secrets or {},
        session_secrets=session_secrets or {},
    )


def _is_briefer_budget_ok_impl(config: Config) -> bool:
    """Check if the briefer should run based on config and LLM budget."""
    if not setting_bool(config.settings, "briefer_enabled"):
        return False
    from kiso.llm import get_llm_call_count

    max_calls = setting_int(config.settings, "max_llm_calls_per_message", lo=1)
    if get_llm_call_count() >= max_calls - 2:
        log.debug(
            "Skipping briefer: LLM budget near limit (%d/%d)",
            get_llm_call_count(),
            max_calls,
        )
        return False
    return True


async def _msg_task_impl(
    config: Config,
    db: aiosqlite.Connection,
    session: str,
    detail: str,
    plan_outputs: list[dict] | None = None,
    goal: str = "",
    include_recent: bool = False,
    user_message: str = "",
    on_briefer_done=None,
    response_lang: str = "",
    briefer_timeout: float = _BRIEFER_MSG_TIMEOUT,
    run_briefer_fn=run_briefer,
    run_messenger_fn=run_messenger,
    selected_skills: list | None = None,
) -> str:
    """Generate a user-facing message via the messenger brain role."""
    if response_lang:
        expected_prefix = f"Answer in {response_lang}."
        if not detail.startswith(expected_prefix):
            if detail.startswith("Answer in "):
                dot_pos = detail.find(".")
                if dot_pos >= 0:
                    detail = detail[dot_pos + 1 :].lstrip()
            detail = f"{expected_prefix} {detail}"

    selected_outputs = plan_outputs
    briefing_context: str | None = None

    if _is_briefer_budget_ok_impl(config):
        try:
            sess = await get_session(db, session)
            session_project_id = await get_session_project_id(db, session)
            facts = await get_facts(
                db,
                session=session,
                limit=_MAX_MESSENGER_FACTS,
                project_id=session_project_id,
            )
            all_tags = await get_all_tags(db)
            all_entities = await get_all_entities(db)
            memory_pack = _build_worker_memory_pack(
                summary=sess["summary"] if sess and sess["summary"] else "",
                facts=facts,
                recent_message=user_message,
                plan_outputs_text=_format_plan_outputs_for_msg(plan_outputs)
                if plan_outputs
                else "",
                goal=goal,
                available_tags=all_tags,
                available_entities=all_entities,
            )
            context_pool: dict = dict(memory_pack.context_sections)
            briefing = await asyncio.wait_for(
                run_briefer_fn(config, "messenger", detail, context_pool, session=session),
                timeout=briefer_timeout,
            )
            if plan_outputs:
                indices = set(briefing.get("output_indices", []))
                if indices:
                    selected_outputs = [o for o in plan_outputs if o["index"] in indices]
                    if not selected_outputs:
                        selected_outputs = plan_outputs
            if briefing.get("context"):
                briefing_context = briefing["context"]
            entity_id = None
            if briefing.get("relevant_entities") and all_entities:
                entity_map = {
                    _normalize_entity_name(e["name"]): e["id"] for e in all_entities
                }
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
                session=session,
                is_admin=False,
                project_id=session_project_id,
            )
            if scored_facts:
                facts_text = "\n".join(f"- {f['content']}" for f in scored_facts)
                if briefing_context:
                    briefing_context += f"\n\n## Relevant Facts\n{facts_text}"
                else:
                    briefing_context = f"## Relevant Facts\n{facts_text}"
        except (BrieferError, LLMError, asyncio.TimeoutError):
            log.debug("Briefer failed for messenger, using full context")

    if on_briefer_done:
        await on_briefer_done()

    outputs_text = _format_plan_outputs_for_msg(selected_outputs) if selected_outputs else ""
    return await run_messenger_fn(
        db,
        config,
        session,
        detail,
        outputs_text,
        goal=goal,
        include_recent=include_recent,
        user_message=user_message,
        briefing_context=briefing_context,
        selected_skills=selected_skills,
    )


def _spawn_knowledge_task_impl(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int | None,
    llm_timeout: int,
    post_plan_knowledge,
) -> asyncio.Task:
    """Spawn _post_plan_knowledge as a background task with its own LLM budget."""

    async def _run() -> None:
        max_calls = setting_int(config.settings, "max_llm_calls_per_message", lo=1)
        set_llm_budget(max_calls)
        reset_usage_tracking()
        try:
            await post_plan_knowledge(db, config, session, plan_id, llm_timeout)
        except Exception:
            log.exception("Background post-plan knowledge failed for session=%s", session)
        finally:
            clear_llm_budget()

    return asyncio.create_task(_run())


async def _maybe_run_consolidation_impl(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    llm_timeout: int,
    run_consolidator_fn=run_consolidator,
    apply_consolidation_result_fn=apply_consolidation_result,
) -> None:
    """Run the consolidator if enough time has passed and enough facts exist."""
    interval_hours = setting_float(config.settings, "consolidation_interval_hours", lo=1.0)
    consolidation_min_facts = setting_int(config.settings, "consolidation_min_facts", lo=1)

    raw_ts = await get_kv(db, _LAST_CONSOLIDATION_KV_KEY)
    last_consolidation = float(raw_ts) if raw_ts else 0.0
    hours_elapsed = (time.time() - last_consolidation) / 3600
    if hours_elapsed < interval_hours:
        return

    all_facts = await get_facts(db, is_admin=True)
    if len(all_facts) < consolidation_min_facts:
        return

    try:
        result = await asyncio.wait_for(
            run_consolidator_fn(config, db, session),
            timeout=llm_timeout,
        )
        await apply_consolidation_result_fn(db, result)
        await set_kv(db, _LAST_CONSOLIDATION_KV_KEY, str(time.time()))
        log.info(
            "Consolidation completed: delete=%d update=%d keep=%d",
            len(result.get("delete", [])),
            len(result.get("update", [])),
            len(result.get("keep", [])),
        )
    except asyncio.TimeoutError:
        log.warning("Consolidation timed out after %ds", llm_timeout)
    except ConsolidatorError as exc:
        log.error("Consolidation failed: %s", exc)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Unexpected error in consolidation phase for session=%s", session)


async def _post_plan_knowledge_impl(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    plan_id: int | None,
    llm_timeout: int,
    apply_curator_result,
    run_curator_fn=run_curator,
    run_summarizer_fn=run_summarizer,
    get_oldest_messages_fn=get_oldest_messages,
    decay_facts_fn=decay_facts,
    archive_low_confidence_facts_fn=archive_low_confidence_facts,
    run_consolidator_fn=run_consolidator,
    apply_consolidation_result_fn=apply_consolidation_result,
) -> None:
    """Run post-plan knowledge processing: curator, summarizer, fact consolidation."""

    async def _run_curator() -> None:
        learnings = await get_pending_learnings(db)
        if not learnings:
            return
        try:
            tags = await get_all_tags(db)
            entities = await get_all_entities(db)
            lower_contents = [l["content"].lower() for l in learnings]
            matched: list[dict] = [
                e for e in entities if any(e["name"] in lc for lc in lower_contents)
            ]
            if matched:
                results = await asyncio.gather(
                    *(
                        search_facts_scored(db, entity_id=e["id"], limit=20)
                        for e in matched
                    )
                )
                relevant_facts: list[dict] = []
                for entity, efacts in zip(matched, results):
                    for fact in efacts:
                        fact["entity_name"] = entity["name"]
                    relevant_facts.extend(efacts)
            else:
                relevant_facts = []
            curator_result = await asyncio.wait_for(
                run_curator_fn(
                    config,
                    learnings,
                    session=session,
                    available_tags=tags,
                    available_entities=entities,
                    existing_facts=relevant_facts or None,
                ),
                timeout=llm_timeout,
            )
            await apply_curator_result(db, session, curator_result)
            await backfill_fact_entities(db)
        except asyncio.TimeoutError:
            log.warning("Curator timed out after %ds", llm_timeout)
        except CuratorError as exc:
            log.error("Curator failed: %s", exc)
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
            oldest = await get_oldest_messages_fn(
                db,
                session,
                limit=min(msg_count, msg_limit),
            )
            new_summary = await asyncio.wait_for(
                run_summarizer_fn(config, current_summary, oldest, session=session),
                timeout=llm_timeout,
            )
            await update_summary(db, session, new_summary)
        except asyncio.TimeoutError:
            log.warning("Summarizer timed out after %ds", llm_timeout)
        except SummarizerError as exc:
            log.error("Summarizer failed: %s", exc)

    await asyncio.gather(_run_curator(), _run_summarizer())

    decay_days = setting_int(config.settings, "fact_decay_days", lo=1)
    decay_rate = setting_float(config.settings, "fact_decay_rate", lo=0.0, hi=1.0)
    archive_threshold = setting_float(
        config.settings, "fact_archive_threshold", lo=0.0, hi=1.0
    )

    async def _run_decay() -> None:
        try:
            decayed = await decay_facts_fn(
                db,
                decay_days=decay_days,
                decay_rate=decay_rate,
            )
            if decayed:
                log.info("Decayed %d stale facts", decayed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Fact decay failed: %s", exc)

    async def _run_archive() -> None:
        try:
            archived = await archive_low_confidence_facts_fn(
                db,
                threshold=archive_threshold,
            )
            if archived:
                log.info("Archived %d low-confidence facts", archived)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Fact archiving failed: %s", exc)

    await asyncio.gather(_run_decay(), _run_archive())

    if setting_bool(config.settings, "consolidation_enabled"):
        await _maybe_run_consolidation_impl(
            db,
            config,
            session,
            llm_timeout,
            run_consolidator_fn=run_consolidator_fn,
            apply_consolidation_result_fn=apply_consolidation_result_fn,
        )

    final_usage = get_usage_summary()
    if plan_id and (final_usage["input_tokens"] or final_usage["output_tokens"]):
        await update_plan_usage(
            db,
            plan_id,
            final_usage["input_tokens"],
            final_usage["output_tokens"],
            final_usage["model"],
        )
