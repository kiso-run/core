"""Worker dispatch handler for MCP tasks.

Takes a plan task with ``type="mcp"``, looks up the configured MCP
manager, calls the named method on the named server, renders the
result into kiso-standard stdout + published files, and flows it
through the same task lifecycle (review → plan_outputs → reviewer
feedback → next task) as wrapper tasks.

Mirrors the shape of ``_handle_wrapper_task`` so the dispatcher's
``_TASK_HANDLERS`` dict can treat it uniformly. Errors are mapped
to the appropriate task status:

- ``MCPInvocationError`` (including isError=true from the server
  and unknown-method errors) → task failed, reviewer sees the
  error text in stdout for its replan decision
- ``MCPTransportError`` → task failed with replan_reason, manager
  has already attempted crash recovery
- ``UnhealthyServerError`` → task failed, the server is circuit-
  broken for this session
- Any other exception → task failed with a generic setup_error
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiso.mcp.result import (
    render_mcp_prompt_result,
    render_mcp_resource_result,
    render_mcp_result,
)
from kiso.mcp.schemas import (
    MCPError,
    MCPInvocationError,
    MCPTransportError,
)
from kiso.mcp.validate import validate_mcp_args
from kiso.worker.utils import _session_workspace

RESOURCE_READ_METHOD = "__resource_read"
PROMPT_GET_METHOD = "__prompt_get"


def _preflight_validate(
    manager: Any, server: str, method: str, args: dict
) -> list[str]:
    """Validate ``args`` against the cached schema for ``server:method``.

    Returns ``[]`` when the schema is absent or the args satisfy it;
    otherwise one error string per violation.
    """
    for m in manager.list_methods_cached_only(server):
        if m.name == method:
            return validate_mcp_args(m.input_schema, args)
    return []

log = logging.getLogger(__name__)


async def _handle_mcp_task(
    ctx: Any,  # _PlanCtx — avoid circular import
    task_row: dict,
    i: int,
    is_final: bool,
    usage_idx_before: int,
) -> Any:  # _TaskHandlerResult
    """Handle a single ``type=mcp`` plan task."""
    # Import here to avoid a circular import at module load
    from kiso.worker.loop import (
        _TaskHandlerResult,
        _audit_task,
        _ensure_task_contract,
        _fail_task_and_audit,
        _log_task_done,
        _review_finalize_ok,
    )
    from kiso.store import update_task

    task_id = task_row["id"]
    _ensure_task_contract(ctx, task_row, i + 1)
    detail = task_row.get("detail") or ""
    server_name = task_row.get("server")
    method_name = task_row.get("method")
    args_raw = task_row.get("args")

    # Pre-flight argument shape
    setup_error: str | None = None
    if not server_name or not isinstance(server_name, str):
        setup_error = "MCP task missing server name"
    elif not method_name or not isinstance(method_name, str):
        setup_error = "MCP task missing method name"
    elif ctx.mcp_manager is None:
        setup_error = "MCP manager not configured on this worker"
    elif not ctx.mcp_manager.is_available(server_name):
        setup_error = (
            f"MCP server {server_name!r} is not available "
            f"(not configured, disabled, or marked unhealthy)"
        )

    if args_raw is None:
        args: dict = {}
    elif isinstance(args_raw, dict):
        args = dict(args_raw)
    elif isinstance(args_raw, str):
        import json as _json
        try:
            parsed = _json.loads(args_raw or "{}")
        except _json.JSONDecodeError as e:
            setup_error = f"MCP args JSON decode failed: {e}"
            args = {}
        else:
            if isinstance(parsed, dict):
                args = parsed
            else:
                setup_error = "MCP args must be a JSON object"
                args = {}
    else:
        setup_error = "MCP args must be a JSON object"
        args = {}

    if setup_error:
        return await _fail_task_and_audit(
            ctx, task_id, "mcp", detail, setup_error, i + 1,
            replan_reason=f"MCP task setup failed: {setup_error}",
        )

    is_resource_read = method_name == RESOURCE_READ_METHOD
    is_prompt_get = method_name == PROMPT_GET_METHOD
    prompt_name: str | None = None
    prompt_args: dict = {}
    if is_resource_read:
        uri = args.get("uri") if isinstance(args, dict) else None
        if not isinstance(uri, str) or not uri:
            msg = (
                f"MCP {server_name}:{RESOURCE_READ_METHOD} requires "
                f"a non-empty string 'uri' arg"
            )
            return await _fail_task_and_audit(
                ctx, task_id, "mcp", detail, msg, i + 1,
                replan_reason=msg,
            )
    elif is_prompt_get:
        pname_raw = args.get("name") if isinstance(args, dict) else None
        if not isinstance(pname_raw, str) or not pname_raw:
            msg = (
                f"MCP {server_name}:{PROMPT_GET_METHOD} requires "
                f"a non-empty string 'name' arg"
            )
            return await _fail_task_and_audit(
                ctx, task_id, "mcp", detail, msg, i + 1,
                replan_reason=msg,
            )
        prompt_name = pname_raw
        pargs_raw = args.get("prompt_args", {}) if isinstance(args, dict) else {}
        if pargs_raw is None:
            prompt_args = {}
        elif isinstance(pargs_raw, dict):
            prompt_args = dict(pargs_raw)
        else:
            msg = (
                f"MCP {server_name}:{PROMPT_GET_METHOD} 'prompt_args' "
                f"must be a JSON object"
            )
            return await _fail_task_and_audit(
                ctx, task_id, "mcp", detail, msg, i + 1,
                replan_reason=msg,
            )
    else:
        schema_errors = _preflight_validate(
            ctx.mcp_manager, server_name, method_name, args
        )
        if schema_errors:
            joined = "; ".join(schema_errors)
            msg = (
                f"MCP {server_name}:{method_name} args invalid: {joined}"
            )
            return await _fail_task_and_audit(
                ctx, task_id, "mcp", detail, msg, i + 1,
                replan_reason=msg,
            )

    t0 = time.perf_counter()
    pub_dir = _session_workspace(ctx.session) / "pub"

    try:
        if is_resource_read:
            blocks = await ctx.mcp_manager.read_resource(
                server_name, args["uri"],
                session=ctx.session,
                sandbox_uid=ctx.sandbox_uid,
            )
            raw_result = render_mcp_resource_result(
                server_name, args["uri"], task_id, pub_dir, blocks,
            )
        elif is_prompt_get:
            rendered = await ctx.mcp_manager.get_prompt(
                server_name, prompt_name, prompt_args,
                session=ctx.session,
                sandbox_uid=ctx.sandbox_uid,
            )
            raw_result = render_mcp_prompt_result(
                server_name, prompt_name, rendered,
            )
        else:
            raw_result = await ctx.mcp_manager.call_method(
                server_name, method_name, args,
                session=ctx.session,
                sandbox_uid=ctx.sandbox_uid,
            )
        # Some managers return a CallResult directly; some return the
        # raw dict. Adapter: both are supported. The test stubs return
        # MCPCallResult-like objects but real MCPManager.call_method
        # returns MCPCallResult built inside the client. Either way we
        # need the raw content[] to do workspace-aware rendering, so
        # the contract in this module assumes the result is an already-
        # rendered MCPCallResult. The client implementations are
        # responsible for building it via _build_call_result at the
        # transport layer — we just forward it here.
    except MCPInvocationError as e:
        err_text = str(e)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return await _fail_task_and_audit(
            ctx, task_id, "mcp", detail,
            err_text, i + 1,
            replan_reason=f"MCP {server_name}:{method_name} error: {err_text}",
        )
    except MCPTransportError as e:
        err_text = str(e)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return await _fail_task_and_audit(
            ctx, task_id, "mcp", detail,
            err_text, i + 1,
            replan_reason=f"MCP {server_name} transport failure: {err_text}",
        )
    except MCPError as e:
        err_text = str(e)
        return await _fail_task_and_audit(
            ctx, task_id, "mcp", detail,
            err_text, i + 1,
            replan_reason=f"MCP {server_name}: {err_text}",
        )
    except Exception as e:  # noqa: BLE001
        return await _fail_task_and_audit(
            ctx, task_id, "mcp", detail,
            f"MCP unexpected failure: {e}", i + 1,
            replan_reason=f"MCP {server_name}:{method_name} unexpected: {e}",
        )

    # raw_result may already be an MCPCallResult from the client, or a
    # raw dict from a lower-level transport. Handle both.
    if hasattr(raw_result, "stdout_text"):
        call_result = raw_result
    else:
        call_result = render_mcp_result(
            server_name, method_name, task_id, pub_dir, raw_result
        )

    if call_result.is_error:
        return await _fail_task_and_audit(
            ctx, task_id, "mcp", detail,
            call_result.stdout_text or "mcp tool returned isError",
            i + 1,
            replan_reason=f"MCP {server_name}:{method_name} returned isError",
        )

    stdout = call_result.stdout_text
    duration_ms = int((time.perf_counter() - t0) * 1000)
    await update_task(ctx.db, task_id, "done", output=stdout, duration_ms=duration_ms)
    _audit_task(ctx, task_id, "mcp", detail, "done", duration_ms, len(stdout))
    _log_task_done(ctx, task_id, "mcp", "done", duration_ms)

    local_plan_output = {
        "index": i + 1,
        "type": "mcp",
        "detail": detail,
        "output": stdout,
        "status": "done",
    }
    return await _review_finalize_ok(
        ctx, task_id, task_row, None, local_plan_output, usage_idx_before
    )
