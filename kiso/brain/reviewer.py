"""Reviewer-specific validation and review prompt assembly."""

from __future__ import annotations

import logging
import re

from kiso.config import Config
from kiso.security import fence_content

from .common import (
    REVIEW_SCHEMA,
    REVIEW_STATUSES,
    REVIEW_STATUS_OK,
    REVIEW_STATUS_REPLAN,
    REVIEW_STATUS_STUCK,
    ReviewError,
    _build_messages,
    _join_or_empty,
    _load_modular_prompt,
    _retry_llm_with_validation,
)

log = logging.getLogger("kiso.brain")

_IMPORTED_NAMES = set(globals())

def validate_review(review: dict) -> list[str]:
    """Validate review semantics. Returns list of error strings."""
    errors: list[str] = []
    status = review.get("status")
    if status not in REVIEW_STATUSES:
        errors.append(f"status must be 'ok', 'replan', or 'stuck', got {status!r}")
        return errors
    if status in (REVIEW_STATUS_REPLAN, REVIEW_STATUS_STUCK) and not review.get("reason"):
        errors.append(f"{status} status requires a non-null, non-empty reason")
    return errors


# --- Learning quality filters ---

_EPHEMERAL_LEARN_RE = re.compile(
    r"\[\d+\].*\[\d+\]"  # 2+ browser element indices like [8], [9]
)
_TRANSIENT_LEARN_RE = re.compile(
    r"\b(installed|loaded|ran|started|completed|finished)\s+successfully\b",
    re.IGNORECASE,
)
_MIN_LEARN_LEN = 15
_NEG_CLAIM_PATTERNS = (
    "not found", "not available", "not stated",
    "does not support", "not installed",
)


def _learning_contradicts_output(learning: str, output: str) -> bool:
    """Check if a negative-claim learning is contradicted by the task output.

    Returns True when the learning says something is "not found" / "not available"
    but the subject term actually appears in the output.
    """
    learning_lower = learning.lower()
    output_lower = output.lower()
    for neg in _NEG_CLAIM_PATTERNS:
        if neg in learning_lower:
            idx = learning_lower.index(neg)
            subject_words = learning[:idx].strip().split()[-2:]
            if any(
                w.lower() in output_lower
                for w in subject_words
                if len(w) > 3
            ):
                return True
    return False


def clean_learn_items(
    items: list[str], task_output: str | None = None,
) -> list[str]:
    """Filter out low-quality learn items from a reviewer response.

    Removes items that are:
    - Too short (< 15 chars) — fragmentary
    - Contain 2+ browser element indices ``[N]`` — ephemeral session data
    - Match transient patterns like "X installed successfully"
    - Negative claims contradicted by task output
    """
    kept: list[str] = []
    for item in items:
        if len(item) < _MIN_LEARN_LEN:
            log.debug("Learn item filtered (too short): %s", item)
            continue
        if _EPHEMERAL_LEARN_RE.search(item):
            log.debug("Learn item filtered (ephemeral indices): %s", item[:80])
            continue
        if _TRANSIENT_LEARN_RE.search(item):
            log.debug("Learn item filtered (transient): %s", item[:80])
            continue
        if task_output and _learning_contradicts_output(item, task_output):
            log.debug("Learn item filtered (contradicts output): %s", item[:80])
            continue
        kept.append(item)
    return kept


_EXIT_CODE_NOTES: dict[int, str] = {
    1: "Note: exit 1 from grep/which/find/dpkg means 'no matches found', not an error.",
    2: "Note: exit 2 often indicates a usage/syntax error in the command.",
    126: "Note: exit 126 means the command was found but is not executable (permission issue).",
    127: "Note: exit 127 means the command was not found in PATH.",
    -1: "Note: the process was killed (OS error).",
}


_REVIEWER_OUTPUT_LIMIT = 16_000
_REVIEWER_STDERR_BUDGET = 2000
_REVIEWER_STDERR_MAX_LINES = 40
_REVIEWER_MAX_GREP_MATCHES = 20

_ERROR_RE = re.compile(
    r"error|fail|exception|traceback|warning|denied|not found|fatal|panic|refused|timeout|errno",
    re.IGNORECASE,
)

def _sanitize_for_reviewer(text: str) -> str:
    """Strip binary/non-printable content from exec output before sending to reviewer.

    Removes individual non-printable characters (all except printable chars and
    normal whitespace \\t \\n \\r).  Appends a marker if any chars were removed,
    so the reviewer knows output was sanitized.  Normal text and valid Unicode
    are kept unchanged.
    """
    if not text:
        return text
    # Fast path: no suspicious chars
    if "\x00" not in text and "\ufffd" not in text and all(
        c.isprintable() or c in "\t\n\r" for c in text
    ):
        return text

    clean: list[str] = []
    removed = 0
    for c in text:
        # \ufffd is the Unicode replacement char — appears when binary is
        # force-decoded as UTF-8; treat it as non-printable for our purposes.
        if (c.isprintable() or c in "\t\n\r") and c != "\ufffd":
            clean.append(c)
        else:
            removed += 1

    result = "".join(clean)
    if removed:
        result += f"\n[binary content suppressed — {removed} bytes]"
    return result


def prepare_reviewer_output(
    stdout: str, stderr: str, limit: int = _REVIEWER_OUTPUT_LIMIT,
) -> str:
    """Prepare task output for the reviewer LLM.

    For small outputs (≤ *limit*), returns the combined text as-is.
    For large outputs, builds: error section (stderr + error grep) +
    main output (head + truncation marker + tail), all within *limit*.
    """
    stdout = _sanitize_for_reviewer(stdout)
    stderr = _sanitize_for_reviewer(stderr)
    combined = stdout
    if stderr:
        combined += f"\n--- stderr ---\n{stderr}"
    if len(combined) <= limit:
        return combined

    # --- Error section (always present if errors exist) ---
    error_parts: list[str] = []

    # Stderr
    if stderr.strip():
        stderr_lines = stderr.splitlines()[:_REVIEWER_STDERR_MAX_LINES]
        stderr_text = "\n".join(stderr_lines)
        if len(stderr_text) > _REVIEWER_STDERR_BUDGET:
            stderr_text = stderr_text[:_REVIEWER_STDERR_BUDGET] + "\n... (stderr truncated)"
        error_parts.append(f"--- stderr ({len(stderr_lines)} lines) ---\n{stderr_text}")

    # Error grep — scan stdout for error keywords
    stdout_lines = stdout.splitlines()
    grep_matches: list[str] = []
    for i, line in enumerate(stdout_lines):
        if _ERROR_RE.search(line):
            context_line = stdout_lines[i - 1] if i > 0 else ""
            entry = f"{context_line}\n{line}".strip() if context_line else line
            if entry not in grep_matches:
                grep_matches.append(entry)
            if len(grep_matches) >= _REVIEWER_MAX_GREP_MATCHES:
                break

    if grep_matches:
        grep_text = "\n".join(grep_matches)
        error_parts.append(f"--- error matches ---\n{grep_text}")

    error_section = "\n".join(error_parts)

    # --- Main output (head + truncation marker + tail) ---
    # Reserve space for error section + separator
    error_overhead = len(error_section) + 5 if error_section else 0  # "\n---\n"
    main_budget = limit - error_overhead

    if len(stdout) <= main_budget:
        main_section = stdout
    else:
        # Reserve space for marker + newlines (~50 chars)
        usable = main_budget - 50
        half = max(usable // 2, 100)
        skipped = len(stdout) - half * 2
        main_section = (
            f"{stdout[:half]}\n\n"
            f"[... {skipped} chars truncated ...]\n\n"
            f"{stdout[-half:]}"
        )

    # --- Combine ---
    if error_section:
        result = f"{error_section}\n---\n{main_section}"
    else:
        result = main_section

    # Hard cap (should rarely hit after budget calculation)
    if len(result) > limit:
        result = result[:limit]

    return result


def _select_reviewer_modules(
    output: str, safety_rules: list[str] | None,
) -> list[str]:
    """Heuristic module selection for reviewer (no briefer call needed).

    Always includes ``rules``.  Adds ``learn_quality`` when output is
    non-trivial, and ``compliance`` when safety rules are present.
    """
    modules: list[str] = ["rules"]
    if output and len(output.strip()) > 20:
        modules.append("learn_quality")
    if safety_rules:
        modules.append("compliance")
    return modules


def build_reviewer_messages(
    goal: str,
    detail: str,
    expect: str,
    output: str,
    user_message: str,
    success: bool | None = None,
    exit_code: int | None = None,
    safety_rules: list[str] | None = None,
    selected_skills: "list | None" = None,
) -> list[dict]:
    """Build the message list for the reviewer LLM call.

    M1540: ``selected_skills`` with ``## Reviewer`` sections are
    injected as ``## Skills (reviewer heuristics)`` AFTER
    ``## Expected Outcome`` and BEFORE ``## Actual Output``. They
    supplement but do not replace ``expect`` — the reviewer prompt
    keeps ``expect`` as the primary pass/fail criterion.
    """
    modules = _select_reviewer_modules(output, safety_rules)
    system_prompt = _load_modular_prompt("reviewer", modules)

    skills_block = ""
    if selected_skills:
        from kiso.skill_runtime import instructions_for_reviewer
        blocks: list[str] = []
        for skill in selected_skills:
            body = instructions_for_reviewer(skill).strip()
            if body:
                blocks.append(f"### {skill.name}\n{body}")
        if blocks:
            skills_block = (
                "\n\n## Skills (reviewer heuristics)\n\n"
                + "\n\n".join(blocks)
            )

    context = (
        f"## Plan Context\n{goal}\n\n"
        f"## Task Detail\n{detail}\n\n"
        f"## Expected Outcome\n{expect}"
        f"{skills_block}\n\n"
        f"## Actual Output\n{fence_content(output, 'TASK_OUTPUT')}\n\n"
        f"## Original User Message\n{fence_content(user_message, 'USER_MSG')}"
    )

    if success is not None:
        if exit_code is not None:
            if success:
                status_text = f"Exit code: 0 (success)"
            else:
                note = _EXIT_CODE_NOTES.get(exit_code, "")
                status_text = f"Exit code: {exit_code} (non-zero)"
                if note:
                    status_text += f"\n{note}"
        else:
            status_text = "succeeded (exit code 0)" if success else "FAILED (non-zero exit code)"
        context += f"\n\n## Command Status\n{status_text}"

    # inject safety rules for compliance check
    rules_text = _join_or_empty(safety_rules)
    if rules_text:
        context += f"\n\n## Safety Rules (violations → stuck)\n{rules_text}"

    return _build_messages(system_prompt, context)


async def run_reviewer(
    config: Config,
    goal: str,
    detail: str,
    expect: str,
    output: str,
    user_message: str,
    session: str = "",
    success: bool | None = None,
    exit_code: int | None = None,
    safety_rules: list[str] | None = None,
) -> dict:
    """Run the reviewer on a task output.

    Returns dict with keys: status ("ok" | "replan"), reason, learn.
    Raises ReviewError if all retries exhausted.
    """
    messages = build_reviewer_messages(
        goal, detail, expect, output, user_message,
        success=success, exit_code=exit_code,
        safety_rules=safety_rules,
    )
    review = await _retry_llm_with_validation(
        config, "reviewer", messages, REVIEW_SCHEMA,
        validate_review, ReviewError, "Review",
        session=session,
    )
    log.info("Review: status=%s reason=%s", review["status"],
             (review.get("reason") or "")[:200])
    return review


__brain_exports__ = [
    name
    for name in globals()
    if name not in _IMPORTED_NAMES and name not in {"__brain_exports__", "_IMPORTED_NAMES"}
]

del _IMPORTED_NAMES
