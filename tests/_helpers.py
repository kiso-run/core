"""Shared test helpers."""

from __future__ import annotations

from typing import Any


def make_task_dict(
    *,
    type: str = "exec",
    detail: str = "echo hi",
    status: str = "done",
    output: str = "",
    expect: str | None = None,
    args: dict[str, Any] | None = None,
    id: int | None = None,
) -> dict[str, Any]:
    """Return a synthetic task-result dict for tests of post-execution
    consumers (`_build_replan_context`, `_collect_task_results`, the
    plan-output writers).

    Defaults to a 'happy success exec' shape. Optional kwargs
    (`expect`, `args`, `id`) land in the result only when explicitly
    set; this matches how the post-execution consumers read these
    fields (always via ``.get(...)``, treating absent and ``None`` as
    equivalent), but NOT how the planner emits them in plan-task
    JSON, where ``args`` and ``expect`` are required keys with
    explicit ``null`` values per the planner JSON schema. Use this
    factory for completed/result task dicts; do NOT use it to
    synthesize plan-task inputs (those have a different
    ``{type, detail, args, expect}`` shape).
    """
    d: dict[str, Any] = {
        "type": type,
        "detail": detail,
        "status": status,
        "output": output,
    }
    if expect is not None:
        d["expect"] = expect
    if args is not None:
        d["args"] = args
    if id is not None:
        d["id"] = id
    return d
