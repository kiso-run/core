"""Text-producing roles for `kiso.brain`."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import aiosqlite

from kiso.config import Config
from kiso.llm import LLMBudgetExceeded, LLMError, LLMStallError, call_llm
from kiso.security import fence_content
from kiso.store import (
    get_behavior_facts,
    get_facts,
    get_recent_messages,
    get_session,
    get_session_project_id,
)

from .common import (
    MemoryPack,
    _ANSWER_IN_LANG_RE,
    _MAX_MESSENGER_FACTS,
    _MAX_MESSENGER_RETRIES,
    _MESSENGER_RETRY_BACKOFF,
    _add_section,
    _build_messages,
    _build_messages_from_sections,
    _build_messenger_memory_pack,
    _build_worker_memory_pack,
    _join_or_empty,
    _load_system_prompt,
    _require_memory_pack_role,
    build_recent_context,
)
from .curator import SummarizerError

log = logging.getLogger("kiso.brain")

_IMPORTED_NAMES = set(globals())

def build_summarizer_messages(
    current_summary: str, messages: list[dict]
) -> list[dict]:
    """Build the message list for the summarizer LLM call."""
    system_prompt = _load_system_prompt("summarizer")
    parts: list[str] = []
    _add_section(parts, "Current Summary", current_summary)
    parts.append(f"## Messages\n{build_recent_context(messages, kiso_truncate=0)}")
    return _build_messages(system_prompt, "\n\n".join(parts))


@dataclass(frozen=True)
class _TextRoleRetryPolicy:
    """Shared retry/fallback policy for non-structured role calls."""

    fallback_model: str | None = None
    max_retries: int = 0
    retry_backoff: float = 0.0


async def _run_text_role(
    config: Config,
    role: str,
    messages: list[dict],
    error_class: type[Exception],
    session: str = "",
    *,
    policy: _TextRoleRetryPolicy | None = None,
    sanitize_fn: Callable[[str], str] | None = None,
) -> str:
    """Run a text-producing role with shared retry/fallback behavior."""
    policy = policy or _TextRoleRetryPolicy()
    total_attempts = policy.max_retries + 1
    using_fallback = False
    last_err: Exception | None = None

    attempt = 0
    while attempt < total_attempts:
        attempt += 1
        model_override = policy.fallback_model if using_fallback else None
        try:
            text = await call_llm(
                config, role, messages, session=session,
                model_override=model_override,
            )
            return sanitize_fn(text) if sanitize_fn else text
        except LLMBudgetExceeded:
            raise
        except LLMError as e:
            last_err = e
            # Stall or timeout with fallback available → switch model
            is_stall = isinstance(e, LLMStallError)
            should_fallback = (
                (is_stall or "timed out" in str(e))
                and policy.fallback_model
                and not using_fallback
            )
            if should_fallback:
                log.warning("%s on %s, switching to fallback %s",
                            "SSE stall" if is_stall else "Timeout",
                            role, policy.fallback_model)
                using_fallback = True
                if attempt >= total_attempts:
                    total_attempts += 1
                continue
            # Stall without fallback → give up
            if is_stall:
                break
            # Other LLM errors → retry same model
            if attempt < total_attempts:
                log.warning("%s retry %d/%d: %s", role.capitalize(), attempt + 1, policy.max_retries, e)
                if policy.retry_backoff > 0:
                    await asyncio.sleep(policy.retry_backoff)
                continue
            break

    if isinstance(last_err, LLMStallError) and not policy.fallback_model:
        raise error_class("LLM stall with no fallback model")
    if using_fallback and last_err is not None:
        raise error_class(f"Fallback LLM call failed: {last_err}")
    raise error_class(f"LLM call failed after {total_attempts} attempts: {last_err}")


async def _call_role(
    config: Config, role: str, messages: list[dict],
    error_class: type[Exception], session: str = "",
    fallback_model: str | None = None,
    max_retries: int = 0,
) -> str:
    """Call an LLM role and wrap errors in the role-specific exception.

    On ``LLMStallError``, retries once with *fallback_model* if provided.
    """
    return await _run_text_role(
        config,
        role,
        messages,
        error_class,
        session=session,
        policy=_TextRoleRetryPolicy(
            fallback_model=fallback_model,
            max_retries=max_retries,
        ),
    )


async def run_summarizer(
    config: Config, current_summary: str, messages: list[dict], session: str = "",
) -> str:
    """Run the summarizer. Returns the new summary string."""
    msgs = build_summarizer_messages(current_summary, messages)
    return await _call_role(config, "summarizer", msgs, SummarizerError, session)


# ---------------------------------------------------------------------------
# Paraphraser
# ---------------------------------------------------------------------------


class ParaphraserError(Exception):
    """Paraphraser generation failure."""


def build_paraphraser_messages(messages: list[dict]) -> list[dict]:
    """Build the message list for the paraphraser LLM call."""
    system_prompt = _load_system_prompt("paraphraser")
    lines = []
    for m in messages:
        user = m.get("user") or "unknown"
        content = m.get("content", "")
        lines.append(f"[{user}]: {content}")
    return _build_messages(system_prompt, "\n".join(lines))


async def run_paraphraser(config: Config, messages: list[dict], session: str = "") -> str:
    """Run the paraphraser on untrusted messages. Returns paraphrased text."""
    msgs = build_paraphraser_messages(messages)
    return await _call_role(config, "paraphraser", msgs, ParaphraserError, session)


# ---------------------------------------------------------------------------
# Messenger
# ---------------------------------------------------------------------------


class ExecTranslatorError(Exception):
    """Exec-to-shell translation failure."""


class _ExecTranslatorValidationError(Exception):
    """Internal translator validation error with optional targeted-repair metadata."""

    def __init__(self, message: str, *, repair_kind: str | None = None):
        super().__init__(message)
        self.repair_kind = repair_kind


class MessengerError(Exception):
    """Messenger generation failure."""


def build_messenger_messages(
    config: Config,
    summary: str,
    facts: list[dict],
    detail: str,
    plan_outputs_text: str = "",
    goal: str = "",
    recent_messages: list[dict] | None = None,
    user_message: str = "",
    briefing_context: str | None = None,
    behavior_rules: list[str] | None = None,
    memory_pack: MemoryPack | None = None,
) -> list[dict]:
    """Build the message list for the messenger LLM call.

    Args:
        config: Application config (reads bot_name from settings).
        summary: Current session summary.
        facts: Known facts from the knowledge base.
        detail: The msg task detail (what to communicate).
        plan_outputs_text: Pre-formatted preceding task outputs (from worker).
        goal: The plan goal (user's original request for this turn).
        recent_messages: Recent conversation messages (for chat mode context).
        user_message: The original user message (for language/context inference).
        briefing_context: Synthesized context from the briefer (replaces
            raw summary/facts when provided).
    """
    bot_name = config.settings["bot_name"]
    bot_persona = config.settings.get("bot_persona", "a friendly and knowledgeable assistant")
    system_prompt = _load_system_prompt("messenger").replace(
        "{bot_name}", bot_name,
    ).replace(
        "{bot_persona}", bot_persona,
    )
    if memory_pack is not None:
        _require_memory_pack_role(memory_pack, "messenger")
        summary = memory_pack.context_sections.get("summary", summary)
        facts = memory_pack.facts or facts
        recent_messages = memory_pack.recent_messages or recent_messages
        behavior_rules = memory_pack.behavior_rules or behavior_rules

    context_parts: list[str] = []
    # extract language from "Answer in {lang}." prefix and inject as
    # a dedicated top-level section so the LLM cannot miss it.
    _lang_m = _ANSWER_IN_LANG_RE.match(detail)
    if _lang_m:
        context_parts.append(
            f"## Language Directive\nRespond entirely in **{_lang_m.group(1)}**."
        )
    if user_message:
        context_parts.append(
            f"## Original User Message\n{fence_content(user_message, 'USER_MSG')}"
        )
    _add_section(context_parts, "Current User Request", goal)
    if briefing_context:
        # Briefer path: synthesized context replaces raw summary/facts.
        # Fence LLM-generated briefer output to prevent cross-LLM injection.
        context_parts.append(f"## Context\n{fence_content(briefing_context, 'BRIEFER_CONTEXT')}")
    else:
        # Fallback: full raw context
        _add_section(context_parts, "Session Summary (background only)", summary)
        _add_section(context_parts, "Known Facts",
                     _join_or_empty(facts, lambda f: f"- {f['content']}"))
    if recent_messages:
        context_parts.append(
            f"## Recent Conversation\n{fence_content(build_recent_context(recent_messages, kiso_truncate=0), 'MESSAGES')}"
        )
    # inject behavioral guidelines
    if behavior_rules:
        _add_section(context_parts, "Behavior Guidelines (follow these preferences)",
                     "\n".join(f"- {r}" for r in behavior_rules))
    _add_section(context_parts, "Preceding Task Outputs", plan_outputs_text)
    context_parts.append(f"## Task\n{detail}")
    return _build_messages_from_sections(system_prompt, context_parts)


async def run_messenger(
    db: aiosqlite.Connection,
    config: Config,
    session: str,
    detail: str,
    plan_outputs_text: str = "",
    goal: str = "",
    include_recent: bool = False,
    user_message: str = "",
    briefing_context: str | None = None,
) -> str:
    """Run the messenger: generate a user-facing response.

    Loads session summary and facts, builds context, and calls the
    messenger LLM to produce text for the user.

    When *briefing_context* is provided (from the briefer), it replaces
    the raw summary and facts in the messenger prompt.

    Returns the generated text.
    Raises MessengerError on failure.
    """
    summary = ""
    facts: list[dict] = []
    if not briefing_context:
        # Only fetch summary/facts when briefer hasn't already filtered them
        sess = await get_session(db, session)
        summary = sess["summary"] if sess else ""
        session_project_id = await get_session_project_id(db, session)
        facts = await get_facts(
            db,
            session=session,
            limit=_MAX_MESSENGER_FACTS,
            project_id=session_project_id,
        )
    recent = None
    if include_recent:
        context_limit = int(config.settings["context_messages"])
        recent = await get_recent_messages(db, session, limit=context_limit)
    # fetch behavior guidelines for messenger
    behavior_facts = await get_behavior_facts(db)
    behavior_rules = [f["content"] for f in behavior_facts] if behavior_facts else None
    memory_pack = _build_messenger_memory_pack(
        summary=summary,
        facts=facts,
        recent_messages=recent,
        behavior_rules=behavior_rules,
    )
    messages = build_messenger_messages(
        config, summary, facts, detail, plan_outputs_text, goal=goal,
        recent_messages=recent or None, user_message=user_message,
        briefing_context=briefing_context, behavior_rules=behavior_rules,
        memory_pack=memory_pack,
    )
    _fallback = config.settings.get("planner_fallback_model", "minimax/minimax-m2.7")
    return await _run_text_role(
        config,
        "messenger",
        messages,
        MessengerError,
        session=session,
        policy=_TextRoleRetryPolicy(
            fallback_model=_fallback,
            max_retries=_MAX_MESSENGER_RETRIES,
            retry_backoff=_MESSENGER_RETRY_BACKOFF,
        ),
        sanitize_fn=_sanitize_messenger_output,
    )


# strip hallucinated XML/wrapper markup from messenger output
_TOOL_CALL_BLOCK_RE = re.compile(
    r"<(tool_call|function_call)[^>]*>.*?</\1>", re.DOTALL,
)
_TOOL_CALL_TAG_RE = re.compile(r"</?(tool_call|function_call)[^>]*>")

# deterministic emoji strip for messenger output. The
# messenger.md prompt has a CRITICAL no-emoji rule, but smaller
# models (e.g. gemini-2.5-flash) periodically violate it. The
# regression history (, then again 2026-04-09) shows that
# prompt-only enforcement is not reliable, so we apply a
# deterministic strip on every messenger output. The character
# ranges below are kept in sync with the
# ``tests/functional/test_knowledge.py`` regex via a parity test.
EMOJI_STRIP_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA9F]"
)


def strip_emoji(text: str) -> str:
    """Remove emoji characters from *text*.

    Used to enforce the messenger's no-emoji constraint as a hard
    rule in code. Operates on the same character ranges the
    functional test regex covers (``[U+1F300-U+1F9FF]``,
    ``[U+2600-U+27BF]``, ``[U+1FA00-U+1FA9F]``).
    """
    if not text:
        return text
    return EMOJI_STRIP_RE.sub("", text)


def _sanitize_messenger_output(text: str) -> str:
    """Strip hallucinated tool_call/function_call XML and emoji from messenger output."""
    cleaned = _TOOL_CALL_BLOCK_RE.sub("", text)
    cleaned = _TOOL_CALL_TAG_RE.sub("", cleaned)
    cleaned = EMOJI_STRIP_RE.sub("", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


class SearcherError(Exception):
    """Searcher generation failure."""


def build_searcher_messages(
    query: str,
    context: str = "",
    max_results: int | None = None,
    lang: str | None = None,
    country: str | None = None,
) -> list[dict]:
    """Build the message list for the searcher LLM call."""
    system_prompt = _load_system_prompt("searcher")
    parts = [f"## Search Query\n{query}"]
    params: list[str] = []
    if max_results is not None:
        params.append(f"max_results: {max_results}")
    if lang:
        params.append(f"lang: {lang}")
    if country:
        params.append(f"country: {country}")
    if params:
        parts.append("## Search Parameters\n" + "\n".join(params))
    _add_section(parts, "Context", context)
    return _build_messages_from_sections(system_prompt, parts)


async def run_searcher(
    config: Config,
    query: str,
    context: str = "",
    max_results: int | None = None,
    lang: str | None = None,
    country: str | None = None,
    session: str = "",
) -> str:
    """Run the searcher: web search via an online-capable model.

    Returns the raw search results text (not parsed).
    Raises SearcherError on failure.
    """
    messages = build_searcher_messages(
        query, context, max_results=max_results, lang=lang, country=country,
    )
    return await _call_role(config, "searcher", messages, SearcherError, session)


# ---------------------------------------------------------------------------
# Exec translator  (planner = architect, worker/translator = editor)
# ---------------------------------------------------------------------------

def build_exec_translator_messages(
    config: Config,
    detail: str,
    sys_env_text: str,
    plan_outputs_text: str = "",
    retry_context: str = "",
    workspace_files: str = "",
    recipe_contracts_text: str = "",
    selected_skills: "list | None" = None,
) -> list[dict]:
    """Build the message list for the exec translator LLM call.

    M1540: when ``selected_skills`` is provided, their ``## Worker``
    role sections are injected as a ``## Skills (worker guidance)``
    block between ``Preceding Task Outputs`` and ``Task``. Skills
    without a ``## Worker`` section contribute nothing.
    """
    from kiso.skill_runtime import instructions_for_worker

    system_prompt = _load_system_prompt("worker")
    context_parts: list[str] = [f"## System Environment\n{sys_env_text}"]
    _add_section(context_parts, "Workspace Files", workspace_files)
    _add_section(context_parts, "Preceding Task Outputs", plan_outputs_text)
    _add_section(context_parts, "Recipe Contracts", recipe_contracts_text)
    if selected_skills:
        skill_blocks: list[str] = []
        for skill in selected_skills:
            worker_body = instructions_for_worker(skill).strip()
            if worker_body:
                skill_blocks.append(f"### {skill.name}\n{worker_body}")
        if skill_blocks:
            context_parts.append(
                "## Skills (worker guidance)\n\n" + "\n\n".join(skill_blocks)
            )
    _add_section(context_parts, "Retry Context", retry_context)
    context_parts.append(f"## Task\n{detail}")
    return _build_messages_from_sections(system_prompt, context_parts)


def _is_simple_shell_intent(detail: str) -> bool:
    """True for obvious one-step shell intents where scripts are overkill."""
    detail_l = detail.lower().strip()
    simple_markers = (
        "current working directory", "working directory",
        "list all files", "list files", "list directories",
        "print the text", "print text", "echo ",
        "show the contents", "show contents", "cat ",
        "check if", "is installed", "command -v",
    )
    return any(marker in detail_l for marker in simple_markers)


def _build_exec_translator_repair_context(
    detail: str,
    *,
    error_text: str,
    repair_kind: str,
    previous_command: str,
    retry_context: str = "",
) -> str:
    """Build a bounded targeted retry hint for structural translator failures."""
    parts: list[str] = []
    if retry_context.strip():
        parts.append(retry_context.strip())
    parts.append(
        "Targeted repair: the previous translator output was structurally invalid. "
        "Return ONLY the corrected shell command."
    )
    parts.append(f"Previous invalid output:\n{previous_command}")
    parts.append(f"Validation error: {error_text}")
    if repair_kind == "syntax":
        parts.append("Fix the bash syntax error and return the shortest equivalent valid shell command.")
    elif repair_kind == "fences":
        parts.append("Remove markdown fences/comments and return raw shell commands only.")
    elif repair_kind == "natural_language":
        parts.append("Replace the natural-language explanation with the actual shell command only.")
    if _is_simple_shell_intent(detail):
        parts.append("This is a simple one-step task. Prefer a single direct command, not a script or heredoc.")
    parts.append("Never repeat the invalid format.")
    return "\n\n".join(parts)


def _validate_exec_translator_command(command: str) -> None:
    """Validate translator output and raise targeted repair errors when possible."""
    if not command or command == "CANNOT_TRANSLATE":
        raise ExecTranslatorError("Cannot translate task to shell command")

    if "```" in command:
        raise _ExecTranslatorValidationError(
            "Markdown fences in command output",
            repair_kind="fences",
        )

    _ECHO_MARKERS = (
        "Public files:", "Blocked commands:", "Plan limits:",
        "Exec CWD:", "System Environment", "Preceding Task Outputs",
        "## Task", "Available binaries:",
    )
    for marker in _ECHO_MARKERS:
        if marker in command:
            raise ExecTranslatorError(
                f"Prompt echo-back detected ('{marker}' in output)"
            )

    _NL_PREFIXES = (
        "I ", "The ", "Here ", "To ", "Let me", "This ", "Sure",
        "Based on", "First,", "Note:", "Unfortunately",
    )
    first_line = command.split("\n", 1)[0]
    if any(first_line.startswith(p) for p in _NL_PREFIXES):
        raise _ExecTranslatorValidationError(
            f"Natural language in command output: {first_line[:80]}",
            repair_kind="natural_language",
        )


async def run_worker(
    config: Config,
    detail: str,
    sys_env_text: str,
    plan_outputs_text: str = "",
    session: str = "",
    retry_context: str = "",
    workspace_files: str = "",
    recipe_contracts_text: str = "",
) -> str:
    """Translate a natural-language exec task detail into a shell command.

    Returns the shell command string.
    Raises ExecTranslatorError on failure.
    """
    _fallback = config.settings.get("planner_fallback_model", "minimax/minimax-m2.7")
    current_retry_context = retry_context
    for attempt in range(2):
        messages = build_exec_translator_messages(
            config, detail, sys_env_text, plan_outputs_text,
            retry_context=current_retry_context,
            workspace_files=workspace_files,
            recipe_contracts_text=recipe_contracts_text,
        )
        raw = await _call_role(
            config, "worker", messages, ExecTranslatorError, session,
            fallback_model=_fallback,
            max_retries=1,
        )
        command = raw.strip()
        try:
            _validate_exec_translator_command(command)

            # always run bash -n syntax check (was >120 chars only)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-n",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate(input=command.encode())
                if proc.returncode != 0:
                    hint = stderr.decode(errors="replace").strip()
                    raise _ExecTranslatorValidationError(
                        f"Bash syntax error in generated command: {hint}",
                        repair_kind="syntax",
                    )
            except FileNotFoundError:
                pass  # bash not available — skip check
            return command
        except _ExecTranslatorValidationError as e:
            if attempt == 0 and e.repair_kind in {"syntax", "fences", "natural_language"}:
                current_retry_context = _build_exec_translator_repair_context(
                    detail,
                    error_text=str(e),
                    repair_kind=e.repair_kind,
                    previous_command=command,
                    retry_context=retry_context,
                )
                continue
            raise ExecTranslatorError(str(e)) from e
        except ExecTranslatorError as e:
            if "Cannot translate task to shell command" in str(e):
                raise ExecTranslatorError(
                    f"Cannot translate task to shell command: {detail}"
                ) from e
            raise

    raise ExecTranslatorError(f"Cannot translate task to shell command: {detail}")


__brain_exports__ = [
    name
    for name in globals()
    if name not in _IMPORTED_NAMES and name not in {"__brain_exports__", "_IMPORTED_NAMES"}
]

del _IMPORTED_NAMES


# ---------------------------------------------------------------------------
# Dreamer (periodic knowledge consolidation)
# ---------------------------------------------------------------------------
