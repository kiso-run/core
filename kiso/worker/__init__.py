"""kiso.worker — per-session asyncio worker package.

Public API is :func:`run_worker`. All other names are re-exported for
backward compatibility with existing imports and test code.
"""

from kiso.worker.utils import (
    _build_cancel_summary,
    _build_exec_env,
    _build_failure_summary,
    _build_replan_context,
    _cleanup_plan_outputs,
    _ensure_sandbox_user,
    _format_plan_outputs_for_msg,
    _report_pub_files,
    _run_subprocess,
    _session_workspace,
    _truncate_output,
    _write_plan_outputs,
)
from kiso.worker.exec import _exec_task
from kiso.worker.search import _parse_search_args, _search_task
from kiso.worker.skill import _skill_task
from kiso.worker.loop import (
    _apply_curator_result,
    _deliver_webhook_if_configured,
    _execute_plan,
    _fast_path_chat,
    _handle_exec_task,
    _handle_msg_task,
    _handle_replan_task,
    _handle_search_task,
    _handle_skill_task,
    _msg_task,
    _persist_plan_tasks,
    _PlanCtx,
    _post_plan_knowledge,
    _process_message,
    _review_task,
    _run_planning_loop,
    _run_review_step,
    _store_step_usage,
    _TASK_HANDLERS,
    _TaskHandlerResult,
    run_worker,
)

__all__ = [
    # utils
    "_build_cancel_summary",
    "_build_exec_env",
    "_build_failure_summary",
    "_build_replan_context",
    "_cleanup_plan_outputs",
    "_ensure_sandbox_user",
    "_format_plan_outputs_for_msg",
    "_report_pub_files",
    "_run_subprocess",
    "_session_workspace",
    "_truncate_output",
    "_write_plan_outputs",
    # exec
    "_exec_task",
    # search
    "_parse_search_args",
    "_search_task",
    # skill
    "_skill_task",
    # loop
    "_apply_curator_result",
    "_deliver_webhook_if_configured",
    "_execute_plan",
    "_fast_path_chat",
    "_handle_exec_task",
    "_handle_msg_task",
    "_handle_replan_task",
    "_handle_search_task",
    "_handle_skill_task",
    "_msg_task",
    "_persist_plan_tasks",
    "_PlanCtx",
    "_post_plan_knowledge",
    "_process_message",
    "_review_task",
    "_run_planning_loop",
    "_run_review_step",
    "_store_step_usage",
    "_TASK_HANDLERS",
    "_TaskHandlerResult",
    "run_worker",
]
