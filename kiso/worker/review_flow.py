"""Review-oriented worker support helpers extracted from the main loop."""

from __future__ import annotations

import logging

import aiosqlite

from kiso import audit
from kiso.brain import clean_learn_items, prepare_reviewer_output, run_reviewer
from kiso.config import Config
from kiso.llm import get_usage_since
from kiso.store import (
    get_safety_facts,
    save_learning,
    update_task_review,
    update_task_usage,
)

log = logging.getLogger(__name__)


async def _review_task_impl(
    config: Config,
    db: aiosqlite.Connection,
    session: str,
    goal: str,
    task_row: dict,
    user_message: str,
    run_reviewer_fn=run_reviewer,
    audit_mod=audit,
    selected_skills: "list | None" = None,
) -> dict:
    """Review an exec/wrapper task. Returns review dict. Stores learning if present."""
    output = task_row.get("output") or ""
    stderr = task_row.get("stderr") or ""
    full_output = prepare_reviewer_output(output, stderr)

    safety_facts = await get_safety_facts(db)
    safety_rules = [f["content"] for f in safety_facts] if safety_facts else None

    success = task_row.get("status") == "done"
    exit_code = task_row.get("exit_code")
    review = await run_reviewer_fn(
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
        selected_skills=selected_skills,
    )

    learn_raw = review.get("learn")
    if isinstance(learn_raw, list):
        learn_items = learn_raw
    elif isinstance(learn_raw, str):
        log.warning(
            "Reviewer returned learn as string, expected list; wrapping: %r",
            learn_raw[:100],
        )
        learn_items = [learn_raw]
    else:
        learn_items = []
    if not (output.strip() or stderr.strip()):
        if learn_items:
            log.warning(
                "Discarding %d learning(s) for task %d — empty output",
                len(learn_items),
                task_row.get("id", 0),
            )
        learn_items = []
    learn_items = clean_learn_items(learn_items, task_output=full_output)
    has_learning = bool(learn_items)
    for item in learn_items:
        await save_learning(db, item, session)
        log.debug("Learning saved: %s", item[:100])

    audit_mod.log_review(session, task_row.get("id", 0), review["status"], has_learning)

    await update_task_review(
        db,
        task_row["id"],
        review["status"],
        reason=review.get("reason"),
        learning="\n".join(learn_items) if learn_items else None,
    )

    return review


async def _store_step_usage_impl(
    db: aiosqlite.Connection,
    task_id: int,
    usage_idx_before: int,
) -> None:
    """Store per-step token totals on the task row."""
    step_usage = get_usage_since(usage_idx_before)
    await update_task_usage(
        db,
        task_id,
        step_usage["input_tokens"],
        step_usage["output_tokens"],
    )
