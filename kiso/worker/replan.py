"""Replan and delivery-context formatting helpers for the worker."""

from __future__ import annotations

import json

from kiso.security import fence_content
from kiso.worker.state import _collect_task_results, _task_result_from_source

_PLAN_OUTPUTS_BUDGET = 8000
_REPLAN_OUTPUT_LIMIT = 1000
_REPLAN_SEARCH_OUTPUT_LIMIT = 2000
_REPLAN_CONTEXT_CHAR_BUDGET = 20000
_PUB_FILES_MARKER = "Published files:\n"
_FACT_LINE_LIMIT = 20
_FACT_CHAR_LIMIT = 200
_FACT_TOTAL_CAP = 15
_HISTORY_OUTPUT_BUDGET = 3000


def _extract_published_urls(plan_outputs: list[dict]) -> list[str]:
    """Extract published file URL lines from plan outputs."""
    marker = _PUB_FILES_MARKER
    marker_len = len(marker)
    lines: list[str] = []
    for result in _collect_task_results(plan_outputs=plan_outputs):
        raw = result.output or ""
        pos = raw.find(marker)
        if pos < 0:
            continue
        for line in raw[pos + marker_len :].splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and "://" in stripped:
                lines.append(stripped)
            elif not stripped:
                break
    return lines


def _format_plan_outputs_for_msg(
    plan_outputs: list[dict], budget: int = _PLAN_OUTPUTS_BUDGET
) -> str:
    """Format plan outputs as readable text for worker LLM prompts."""
    if not plan_outputs:
        return ""

    pub_urls = _extract_published_urls(plan_outputs)
    full_parts: list[tuple[int, str]] = []
    summary_parts: list[tuple[int, str]] = []
    budget_used = 0

    for result in reversed(_collect_task_results(plan_outputs=plan_outputs)):
        idx = result.task_index
        header = f"[{idx}] {result.task_type}: {result.detail}"
        # raw output is ground truth. Always include it verbatim
        # when it fits the budget, even when a reviewer_summary exists.
        # The reviewer_summary is a budget-fallback, not a replacement:
        # downstream consumers (messenger, replan) need the actual values
        # from the task output to cite them correctly.
        output = result.output or "(no output)"
        full_text = f"{header}\nStatus: {result.status}\n{fence_content(output, 'TASK_OUTPUT')}"

        if budget_used + len(full_text) <= budget:
            full_parts.append((idx, full_text))
            budget_used += len(full_text)
        else:
            # Raw output too large for budget: fall back to reviewer
            # summary if available, otherwise header-only.
            fallback = f"[{idx}] {result.task_type}: {result.detail} -> {result.status}"
            if result.reviewer_summary:
                fallback += f"\n(Summary: {result.reviewer_summary})"
            summary_parts.append((idx, fallback))

    full_parts.sort(key=lambda x: x[0])
    summary_parts.sort(key=lambda x: x[0])

    parts: list[str] = []
    if pub_urls:
        parts.append("## Published Files\n" + "\n".join(pub_urls))
    if summary_parts:
        parts.append("(earlier tasks summarized)\n" + "\n".join(t for _, t in summary_parts))
    parts.extend(t for _, t in full_parts)
    return "\n\n".join(parts)


def _task_type_label(task: dict) -> str:
    """Format task type, suffixing the MCP server:method when relevant."""
    result = _task_result_from_source(task)
    label = result.task_type
    if result.task_type == "mcp":
        server = task.get("server")
        method = task.get("method")
        if server and method:
            label = f"mcp/{server}:{method}"
        elif server:
            label = f"mcp/{server}"
    return label


def _format_task_list(tasks: list[dict], label: str) -> str:
    """Format a task list with label and count."""
    if not tasks:
        return ""
    items = [f"- [{_task_type_label(task)}] {task['detail']}" for task in tasks]
    return f"{label} ({len(tasks)}):\n" + "\n".join(items)


def _smart_truncate(text: str, limit: int) -> str:
    """Truncate text while keeping both head and tail."""
    if len(text) <= limit:
        return text
    marker = "\n[... {} chars truncated ...]\n"
    usable = limit - 30
    half = max(usable // 2, 50)
    skipped = len(text) - half * 2
    return f"{text[:half]}{marker.format(skipped)}{text[-half:]}"


def _facts_from_summaries(completed: list[dict], seen: set[str]) -> list[str]:
    """Extract reviewer summaries (highest priority)."""
    facts: list[str] = []
    for task in completed:
        summary = task.get("reviewer_summary")
        if summary and summary not in seen:
            facts.append(summary)
            seen.add(summary)
    return facts


def _facts_from_registry(output: str, seen: set[str]) -> list[str] | None:
    """Parse JSON registry responses. Returns facts or None if not JSON."""
    if output[:1] not in ("{", "["):
        return None
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "name" in data:
            fact = f"Wrapper/connector '{data['name']}' found in registry"
            if "version" in data:
                fact += f" (v{data['version']})"
            if fact not in seen:
                seen.add(fact)
                return [fact]
            return []
        if isinstance(data, list):
            names = [item.get("name") for item in data if isinstance(item, dict) and "name" in item]
            if names:
                fact = f"Registry contains: {', '.join(names[:10])}"
                if fact not in seen:
                    seen.add(fact)
                    return [fact]
                return []
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None


def _facts_from_output_lines(output: str, seen: set[str]) -> list[str]:
    """Scan for install/error keywords or extract the first useful line."""
    facts: list[str] = []
    for line in output.split("\n")[:_FACT_LINE_LIMIT]:
        line_lower = line.strip().lower()
        if not line_lower:
            continue
        if any(kw in line_lower for kw in ("installed", "available", "not found", "error")):
            fact = line.strip()[:_FACT_CHAR_LIMIT]
            if fact not in seen:
                facts.append(fact)
                seen.add(fact)
            break
    return facts


def _extract_confirmed_facts(completed: list[dict]) -> list[str]:
    """Best-effort extraction of confirmed facts from completed task outputs."""
    seen: set[str] = set()
    normalized = [result.to_dict() for result in _collect_task_results(completed=completed)]
    facts: list[str] = _facts_from_summaries(normalized, seen)

    for task in normalized:
        output = (task.get("output") or "").strip()
        if not output:
            continue
        registry_facts = _facts_from_registry(output, seen)
        if registry_facts is not None:
            facts.extend(registry_facts)
            continue
        facts.extend(_facts_from_output_lines(output, seen))
        if output[:_FACT_CHAR_LIMIT] not in seen and len(output) < _FACT_CHAR_LIMIT and task.get("status") == "done":
            first_line = output.split("\n")[0].strip()[:_FACT_CHAR_LIMIT]
            if first_line and first_line not in seen:
                facts.append(first_line)
                seen.add(first_line)

    return facts[:_FACT_TOTAL_CAP]


def _format_replan_hints(update_hints: list[str] | None, replan_history: list[dict]) -> list[str]:
    """Build user updates and suggested-fixes sections."""
    parts: list[str] = []
    if update_hints:
        bullets = "\n".join(f"- {hint}" for hint in update_hints)
        parts.append("## User Updates (received during execution — apply these changes)\n" + bullets)
    all_hints: list[str] = []
    seen_hints: set[str] = set()
    for history in replan_history:
        for hint in history.get("retry_hints", []):
            if hint not in seen_hints:
                all_hints.append(hint)
                seen_hints.add(hint)
    if all_hints:
        bullets = "\n".join(f"- {hint}" for hint in all_hints)
        parts.append(
            "## Suggested Fixes (from reviewer — execute these, do NOT re-investigate)\n"
            + bullets
        )
    return parts


def _format_replan_facts(completed: list[dict], replan_history: list[dict]) -> str | None:
    """Build the confirmed-facts section from completed tasks and history."""
    all_completed = [result.to_dict() for result in _collect_task_results(completed=completed)]
    for history in replan_history:
        for task_result in history.get("task_results", []):
            if isinstance(task_result, dict):
                all_completed.append(task_result)
        for key_output in history.get("key_outputs", []):
            if key_output.startswith("[") and "] " in key_output:
                output_text = key_output[key_output.index("] ") + 2 :]
                all_completed.append({"type": "exec", "output": output_text, "status": "done"})
    confirmed = _extract_confirmed_facts(all_completed)
    if not confirmed:
        return None
    bullets = "\n".join(f"- {fact}" for fact in confirmed)
    return "## Confirmed Facts (DO NOT re-verify these — they are already established)\n" + bullets


def _format_replan_tasks(completed: list[dict], remaining: list[dict]) -> list[str]:
    """Build completed-tasks and remaining-tasks sections."""
    parts: list[str] = []
    if completed:
        items = []
        total_chars = 0
        for result in _collect_task_results(completed=completed):
            result_dict = result.to_dict()
            limit = _REPLAN_SEARCH_OUTPUT_LIMIT if result.task_type == "search" else _REPLAN_OUTPUT_LIMIT
            if total_chars >= _REPLAN_CONTEXT_CHAR_BUDGET:
                items.append(f"- [{_task_type_label(result_dict)}] {result.detail}: {result.status}")
                continue
            if result.reviewer_summary:
                output_text = f"Summary: {result.reviewer_summary}"
            else:
                output_text = fence_content(_smart_truncate(result.output or "", limit), "TASK_OUTPUT") if result.output else "(no output)"
            item = f"- [{_task_type_label(result_dict)}] {result.detail}: {result.status} →\n{output_text}"
            items.append(item)
            total_chars += len(item)
        parts.append("## Completed Tasks\n" + "\n".join(items))
    if remaining:
        items = [f"- [{_task_type_label(task)}] {task['detail']}" for task in remaining]
        parts.append("## Remaining Tasks (not executed)\n" + "\n".join(items))
    return parts


def _format_replan_history(replan_history: list[dict]) -> str | None:
    """Build the previous-replan-attempts section with bounded output size."""
    if not replan_history:
        return None
    items = []
    output_chars = 0
    for history in replan_history:
        tried = ", ".join(history.get("what_was_tried", [])) or "nothing"
        entry = f"- Goal: {history['goal']}, Tried: {tried}, Failure: {history['failure']}"
        if history.get("failure_classes"):
            entry += f"\n  Failure classes: {', '.join(history['failure_classes'])}"
        for hint in history.get("retry_hints", []):
            entry += f"\n  Reviewer hint: {hint}"
        if history.get("no_retry_count") and not history.get("retry_hints"):
            entry += "\n  Note: reviewer indicated no retry possible — try an alternative approach or explain to user."
        for summary in history.get("reviewer_summaries", [])[:2]:
            entry += f"\n  Reviewer summary: {summary[:300]}"
        if history.get("task_results"):
            summary_results = []
            for task_result in history["task_results"][:3]:
                if not isinstance(task_result, dict):
                    continue
                result = _task_result_from_source(task_result)
                summary_results.append(f"[{result.task_index}] {result.task_type}:{result.status}")
            if summary_results:
                entry += f"\n  Task results: {', '.join(summary_results)}"
        for key_output in history.get("key_outputs", []):
            if output_chars >= _HISTORY_OUTPUT_BUDGET:
                break
            truncated = _smart_truncate(key_output, min(_HISTORY_OUTPUT_BUDGET - output_chars, 500))
            entry += f"\n  Output: {truncated}"
            output_chars += len(truncated)
        items.append(entry)
    return "## Previous Replan Attempts (DO NOT repeat these approaches)\n" + "\n".join(items)


def _build_replan_context(
    completed: list[dict],
    remaining: list[dict],
    replan_reason: str,
    replan_history: list[dict],
    update_hints: list[str] | None = None,
) -> str:
    """Build extra context for replanning."""
    completed = [
        result.to_dict()
        for result in _collect_task_results(completed=completed)
        if (result.contract.delivery_mode if result.contract else None) != "user-facing"
        and result.task_type != "msg"
    ]
    parts: list[str] = []
    parts.extend(_format_replan_hints(update_hints, replan_history))
    facts_section = _format_replan_facts(completed, replan_history)
    if facts_section:
        parts.append(facts_section)
    parts.extend(_format_replan_tasks(completed, remaining))
    parts.append(f"## Failure Reason\n{replan_reason}")
    history_section = _format_replan_history(replan_history)
    if history_section:
        parts.append(history_section)
    return "\n\n".join(parts)


def _build_cancel_summary(completed: list[dict], remaining: list[dict], goal: str) -> str:
    """Build a detail string summarizing a cancellation."""
    parts: list[str] = [f"The user cancelled the plan: {goal}"]
    completed_text = _format_task_list(completed, "Completed")
    parts.append(completed_text or "No tasks were completed.")
    skipped_text = _format_task_list(remaining, "Skipped")
    if skipped_text:
        parts.append(skipped_text)
    parts.append("Generate a brief message: what was done, what wasn't, and suggest next steps.")
    return "\n\n".join(parts)


def _derive_stuck_category(reason: str | None) -> str:
    """Classify a failure/stuck reason into a coarse category.

    The category drives how ``_build_failure_summary`` formats the
    detail string it hands to the messenger. Currently only the
    ``safety_violation`` category triggers special-case redaction;
    all other reasons fall through to ``"other"`` and get the
    standard detailed failure summary.

    Rationale: safety-rule stuck reasons contain exactly the paths,
    secrets, or blocked content the rule is meant to hide. If we
    interpolate the raw reason into the messenger's detail string,
    the LLM composing the user-facing refusal paraphrases the
    forbidden content back into the response — violating the very
    rule that produced the stuck. Same architectural pattern as
    the reviewer.reason leak into the replan user message fixed
    earlier: internal enforcement metadata must not leak into the
    presentation layer.
    """
    if not reason:
        return "other"
    if "safety" in reason.lower():
        return "safety_violation"
    return "other"


_SAFETY_REFUSAL_DIRECTIVE = (
    "A safety rule blocked the requested operation. Generate a brief "
    "message acknowledging the constraint WITHOUT naming, listing, or "
    "paraphrasing any specific paths, values, file names, or content "
    "the task was attempting to reveal. The user already knows what "
    "they asked for — do not echo it back. Describe the refusal "
    "generically and suggest a rephrasing or a different approach."
)


def _build_failure_summary(
    completed: list[dict],
    remaining: list[dict],
    goal: str,
    reason: str | None = None,
) -> str:
    """Build a detail string summarizing a plan failure.

    For safety-violation reasons, the function enters a redacted
    branch that omits the raw reason text and omits individual task
    details (which may themselves contain the forbidden content from
    the planner's instructions) — only counts and a generic refusal
    directive reach the messenger. For all other failure categories,
    the existing detailed summary is preserved unchanged.
    """
    category = _derive_stuck_category(reason)
    parts: list[str] = [f"The plan failed: {goal}"]

    if category == "safety_violation":
        parts.append(_SAFETY_REFUSAL_DIRECTIVE)
        if completed:
            parts.append(
                f"Completed tasks: {len(completed)} "
                f"(do not list them, do not say they failed)"
            )
        if remaining:
            parts.append(f"Remaining tasks: {len(remaining)} (do not list them)")
        return "\n\n".join(parts)

    if reason:
        parts.append(f"Failure reason: {reason}")
    completed_text = _format_task_list(completed, "Completed successfully")
    parts.append(completed_text or "No tasks were completed.")
    if completed and not remaining:
        parts.append(
            "All planned tasks completed successfully. The failure occurred during re-planning for the next phase."
        )
    failed_text = _format_task_list(remaining, "Failed/Skipped")
    if failed_text:
        parts.append(failed_text)
    parts.append(
        "Generate a brief message explaining what went wrong and suggest next steps. Completed tasks SUCCEEDED — do NOT say they failed. Focus the error on the failure reason only."
    )
    return "\n\n".join(parts)


# user-visible replan messages must NOT interpolate reviewer.reason
# or replan_history text. Both are internal metadata produced by LLMs in
# diagnostic mode and can contain failure language, absolute filesystem
# paths, command names, and other sensitive details. The reason remains
# available in logs and in the planner's context for the replan itself;
# the user only sees a neutral phase indicator.
_REPLAN_TEMPLATES: dict[str, str] = {
    "investigating": "Investigating... ({depth}/{max})",
    "replanning": "Replanning (attempt {depth}/{max})",
    "stuck": (
        "I'm having trouble with this request. "
        "I've tried replanning {depth} times without success.\n"
        "Can you help me with more details or a different approach?"
    ),
}


def get_replan_message(
    kind: str,
    depth: int,
    max_depth: int,
    reason: str = "",
    tried: str = "",
) -> str:
    """Get a replan notification message.

    Note: *reason* and *tried* are accepted for backward compatibility
    with call sites but are intentionally NOT interpolated into the
    user-visible output. They remain available in logs.
    """
    template = _REPLAN_TEMPLATES[kind]
    return template.format(depth=depth, max=max_depth)
