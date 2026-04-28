"""Plan and task persistence helpers."""

from __future__ import annotations

import json
from typing import cast

import aiosqlite

from .shared import (
    _KEEP_LLM_CALLS,
    _rows_to_dicts,
    _serialize_llm_calls,
    _serialize_task_args,
    _update_field,
    _update_fields,
)


async def create_plan(
    db: aiosqlite.Connection,
    session: str,
    message_id: int,
    goal: str,
    parent_id: int | None = None,
) -> int:
    cur = await db.execute(
        "INSERT INTO plans (session, message_id, goal, parent_id) VALUES (?, ?, ?, ?)",
        (session, message_id, goal, parent_id),
    )
    await db.commit()
    return cast(int, cur.lastrowid)


async def update_task(
    db: aiosqlite.Connection,
    task_id: int,
    status: str,
    output: str | None = None,
    stderr: str | None = None,
    duration_ms: int | None = None,
) -> None:
    await _update_fields(db, "tasks", {
        "status": status, "output": output, "stderr": stderr,
        "duration_ms": duration_ms,
    }, task_id)


async def update_task_review(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: str,
    reason: str | None = None,
    learning: str | None = None,
) -> None:
    await _update_fields(db, "tasks", {
        "review_verdict": verdict, "review_reason": reason,
        "review_learning": learning,
    }, task_id)


async def update_task_command(
    db: aiosqlite.Connection, task_id: int, command: str,
) -> None:
    await _update_field(db, "tasks", "command", command, task_id, update_timestamp=True)


async def update_task_usage(
    db: aiosqlite.Connection,
    task_id: int,
    input_tokens: int,
    output_tokens: int,
    llm_calls: list[dict] | None | object = _KEEP_LLM_CALLS,
) -> None:
    update, calls_json = _serialize_llm_calls(llm_calls)
    if update:
        await db.execute(
            "UPDATE tasks SET input_tokens = ?, output_tokens = ?, llm_calls = ? WHERE id = ?",
            (input_tokens, output_tokens, calls_json, task_id),
        )
    else:
        await db.execute(
            "UPDATE tasks SET input_tokens = ?, output_tokens = ? WHERE id = ?",
            (input_tokens, output_tokens, task_id),
        )
    await db.commit()


async def update_task_substatus(
    db: aiosqlite.Connection, task_id: int, substatus: str,
) -> None:
    await _update_field(db, "tasks", "substatus", substatus, task_id, update_timestamp=True)


async def update_task_retry_count(
    db: aiosqlite.Connection, task_id: int, retry_count: int,
) -> None:
    await _update_field(db, "tasks", "retry_count", retry_count, task_id, update_timestamp=True)


async def append_task_llm_call(
    db: aiosqlite.Connection, task_id: int, call_data: dict,
) -> None:
    await db.execute(
        "UPDATE tasks "
        "SET llm_calls = json_insert("
        "    CASE WHEN json_valid(llm_calls) THEN llm_calls ELSE '[]' END,"
        "    '$[#]', json(?)"
        ") WHERE id = ?",
        (json.dumps(call_data), task_id),
    )
    await db.commit()


async def update_plan_status(
    db: aiosqlite.Connection, plan_id: int, status: str,
) -> None:
    await _update_field(db, "plans", "status", status, plan_id)


async def update_plan_goal(
    db: aiosqlite.Connection, plan_id: int, goal: str,
) -> None:
    await _update_field(db, "plans", "goal", goal, plan_id)


async def update_plan_install_proposal(
    db: aiosqlite.Connection, plan_id: int, value: bool = True,
) -> None:
    await _update_field(db, "plans", "install_proposal", int(value), plan_id)


async def update_plan_awaits_input(
    db: aiosqlite.Connection, plan_id: int, value: bool = True,
) -> None:
    await _update_field(db, "plans", "awaits_input", int(value), plan_id)


async def update_plan_usage(
    db: aiosqlite.Connection,
    plan_id: int,
    input_tokens: int,
    output_tokens: int,
    model: str | None = None,
    llm_calls: list[dict] | None | object = _KEEP_LLM_CALLS,
) -> None:
    update, calls_json = _serialize_llm_calls(llm_calls)
    if update:
        await db.execute(
            "UPDATE plans SET total_input_tokens = ?, total_output_tokens = ?, model = ?, llm_calls = ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, model, calls_json, plan_id),
        )
    else:
        await db.execute(
            "UPDATE plans SET total_input_tokens = ?, total_output_tokens = ?, model = ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, model, plan_id),
        )
    await db.commit()


async def get_tasks_for_plan(db: aiosqlite.Connection, plan_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM tasks WHERE plan_id = ? ORDER BY id", (plan_id,),
    )
    return await _rows_to_dicts(cur)


async def create_task(
    db: aiosqlite.Connection,
    plan_id: int,
    session: str,
    type: str,
    detail: str,
    args: str | dict | None = None,
    expect: str | None = None,
    parallel_group: int | None = None,
    server: str | None = None,
    method: str | None = None,
) -> int:
    args = _serialize_task_args(args)
    cur = await db.execute(
        "INSERT INTO tasks (plan_id, session, type, detail, args, expect, parallel_group, server, method) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (plan_id, session, type, detail, args, expect, parallel_group, server, method),
    )
    await db.commit()
    return cast(int, cur.lastrowid)
