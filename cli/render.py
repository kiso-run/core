"""Terminal renderer for CLI display."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import NoReturn


def die(msg: str) -> NoReturn:
    """Print an error message to stderr and exit with code 1."""
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


@dataclass(frozen=True, slots=True)
class TermCaps:
    """Terminal capabilities detected at startup."""

    color: bool  # 256-color support
    unicode: bool  # UTF-8 icons
    width: int  # terminal columns
    height: int  # terminal rows
    tty: bool  # stdout is a TTY


def detect_caps() -> TermCaps:
    """Detect terminal capabilities from environment."""
    tty = sys.stdout.isatty()
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    color = tty and ("256color" in term or bool(colorterm))
    lang = os.environ.get("LC_ALL", "") or os.environ.get("LANG", "")
    unicode = "utf-8" in lang.lower() or "utf8" in lang.lower()
    try:
        size = os.get_terminal_size()
        width, height = size.columns, size.lines
    except (ValueError, OSError):
        width, height = 80, 24
    return TermCaps(color=color, unicode=unicode, width=width, height=height, tty=tty)


# ── ANSI helpers ─────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"
CLEAR_LINE = "\033[2K"


def _sym(u: str, a: str, caps: TermCaps) -> str:
    """Return unicode or ascii symbol based on terminal capabilities."""
    return u if caps.unicode else a


def _style(text: str, *codes: str, caps: TermCaps) -> str:
    if not caps.color:
        return text
    return "".join(codes) + text + _RESET


class _AsciiBuffer:
    """StringIO wrapper that advertises ``encoding = "ascii"``.

    When passed as *file* to :class:`rich.console.Console`, this forces rich
    to pick ASCII box-drawing characters (``+``, ``-``, ``|``) instead of
    Unicode ones (``━``, ``┃``, …) — useful for terminals that lack UTF-8.
    """

    encoding = "ascii"

    def __init__(self) -> None:
        from io import StringIO
        self._buf = StringIO()

    # Proxy the four methods Console actually calls on its file object.
    def write(self, s: str) -> int:
        return self._buf.write(s)

    def flush(self) -> None:
        self._buf.flush()

    def getvalue(self) -> str:
        return self._buf.getvalue()

    @property
    def writable(self) -> bool:
        return True


def _make_rich_console(caps: TermCaps) -> tuple:
    """Create a Rich Console + buffer for panel/markdown rendering.

    Returns ``(console, buf)`` where *buf* has a ``.getvalue()`` method.
    """
    from rich.console import Console

    use_color = caps.color and caps.tty
    if caps.unicode:
        from io import StringIO
        buf = StringIO()
    else:
        buf = _AsciiBuffer()

    console = Console(
        file=buf,
        width=caps.width,
        force_terminal=use_color,
        no_color=not use_color,
        highlight=False,
    )
    return console, buf


def _rich_buf_to_str(buf) -> str:
    """Convert a Rich buffer to a stripped string (trailing whitespace per line removed)."""
    raw = buf.getvalue()
    lines = [line.rstrip() for line in raw.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _short_model(model: str) -> str:
    """Shorten model name: 'deepseek/deepseek-chat-v3' → 'deepseek-chat-v3'."""
    return model.split("/", 1)[-1] if "/" in model else model


def _labeled_sep(label: str, sep_char: str, width: int = 40) -> str:
    """Build a labeled separator: ``───label────────``."""
    return sep_char * 3 + label + sep_char * max(0, width - 3 - len(label))


def _build_message_parts(messages: list[dict], esc) -> list[str]:
    """Build Rich-markup parts for a list of chat messages (dim, role-labeled)."""
    parts: list[str] = []
    for msg in messages:
        label = msg.get("role", "?")
        content = msg.get("content", "")
        parts.append(f"[dim cyan]\\[{esc(label)}][/dim cyan]")
        parts.append(f"[dim]{esc(content)}[/dim]")
        parts.append("")
    return parts


def _verbose_title(esc, role: str, short_model: str, arrow: str,
                   ts: float | None, detail: str,
                   direction: str = "") -> str:
    """Build a panel title for verbose/inflight LLM call display.

    *direction* is ``"IN"``, ``"OUT"``, or ``""`` (omitted).
    """
    from datetime import datetime, timezone

    ts_str = ""
    if ts:
        ts_str = f" {datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m-%d %H:%M:%S')}"
    dir_label = f" {direction}" if direction else ""
    return f" {esc(role)} {arrow} {esc(short_model)} ({detail}){ts_str}{dir_label} "


def _render_markdown(text: str, caps: TermCaps) -> str:
    """Render *text* as markdown using :pymod:`rich`.

    Behaviour adapts to terminal capabilities:

    * ``caps.color and caps.tty`` → full ANSI (syntax highlighting, bold, …)
    * otherwise → plain text with structure (headings, lists) but no escapes
    * ``caps.unicode is False`` → ASCII box-drawing via :class:`_AsciiBuffer`

    Returns the rendered string with trailing whitespace stripped per line
    (rich pads every line to the full console width).
    """
    if not text:
        return ""
    from rich.markdown import Markdown

    console, buf = _make_rich_console(caps)
    md = Markdown(text, code_theme="monokai")
    console.print(md)
    return _rich_buf_to_str(buf)


# ── Icons ────────────────────────────────────────────────────

_ICONS_UNICODE = {
    "plan": "◆",
    "exec": "▶",
    "skill": "⚡",
    "msg": "💬",
    "ok": "✓",
    "fail": "✗",
    "replan": "↻",
    "search": "🔍",
    "cancel": "⊘",
    "skip": "⊘",
    "thinking": "🤔",
}

_ICONS_ASCII = {
    "plan": "*",
    "exec": ">",
    "skill": "!",
    "msg": '"',
    "ok": "ok",
    "fail": "FAIL",
    "replan": "~>",
    "search": "S",
    "cancel": "X",
    "skip": "-",
    "thinking": "?",
}


def _icon(name: str, caps: TermCaps) -> str:
    table = _ICONS_UNICODE if caps.unicode else _ICONS_ASCII
    return table.get(name, "?")


# ── Spinner ──────────────────────────────────────────────────

_BRAILLE_FRAMES = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
_ASCII_FRAMES = ["|", "/", "-", "\\"]


def spinner_frames(caps: TermCaps) -> list[str]:
    """Return spinner animation frames."""
    return _BRAILLE_FRAMES if caps.unicode else _ASCII_FRAMES


# ── Public render functions ──────────────────────────────────


def render_plan(
    goal: str, task_count: int, caps: TermCaps, *, replan: bool = False,
) -> str:
    """Render plan header line."""
    icon = _icon("replan" if replan else "plan", caps)
    label = "Replan" if replan else "Plan"
    if task_count > 0:
        s = "s" if task_count != 1 else ""
        text = f"{icon} {label}: {goal} ({task_count} task{s})"
    else:
        text = f"{icon} {label}: {goal}"
    return _style(text, _BOLD, _CYAN, caps=caps)


def render_plan_detail(tasks: list[dict], caps: TermCaps) -> str:
    """Render the full task list under the plan header.

    Example output:
      1. [exec] Verify pyproject.toml exists
      2. [exec] Run uv sync
      3. [msg]  Summarize results
    """
    if not tasks:
        return ""
    lines: list[str] = []
    # Reserve space for prefix: "  1. [skill]  " ≈ 16 chars
    max_detail = max(caps.width - 16, 40)
    for i, t in enumerate(tasks, 1):
        ttype = t.get("type", "?")
        detail = (t.get("detail") or "").split("\n", 1)[0].strip()
        # Strip "Answer in <lang>." prefix from msg detail — it's an
        # instruction for the messenger, not useful in the plan overview.
        if ttype == "msg" and detail.startswith("Answer in "):
            dot = detail.find(".", 10)
            if dot != -1:
                detail = detail[dot + 1 :].strip()
        if len(detail) > max_detail:
            detail = detail[: max_detail - 1] + "…"
        # Pad type label for alignment
        label = f"[{ttype}]"
        line = f"  {i}. {label:8s} {detail}" if detail else f"  {i}. {label}"
        lines.append(_style(line, _DIM, caps=caps))
    return "\n".join(lines)


def render_command(command: str, caps: TermCaps) -> str:
    """Render a translated shell command line (e.g. '  $ ls -la')."""
    text = f"  $ {command}"
    return _style(text, _DIM, _CYAN, caps=caps)


def render_usage(plan: dict, caps: TermCaps) -> str:
    """Render token usage summary with cost estimate.

    Example: ⟨ 1,234 in → 567 out │ $0.003 │ deepseek/deepseek-v3.2 ⟩
    """
    input_tokens = plan.get("total_input_tokens", 0) or 0
    output_tokens = plan.get("total_output_tokens", 0) or 0
    model = plan.get("model") or ""
    if not input_tokens and not output_tokens:
        return ""
    in_str = f"{input_tokens:,}"
    out_str = f"{output_tokens:,}"
    if caps.unicode:
        text = f"⟨ {in_str} in → {out_str} out"
    else:
        text = f"< {in_str} in -> {out_str} out"
    # Cost estimate
    if model:
        from kiso.stats import compute_cost
        cost = compute_cost(model, input_tokens, output_tokens)
        sep = "│" if caps.unicode else "|"
        if cost is not None:
            text += f" {sep} ${cost:.4f}"
        text += f" {sep} {model}"
    text += " ⟩" if caps.unicode else " >"
    return _style(text, _DIM, caps=caps)


def render_max_replan(depth: int, caps: TermCaps) -> str:
    """Render max-replan-reached message."""
    icon = _icon("cancel", caps)
    text = f"{icon} Max replans reached ({depth}). Giving up."
    return _style(text, _BOLD, _RED, caps=caps)


def render_task_header(
    task: dict,
    index: int,
    total: int,
    caps: TermCaps,
    *,
    spinner_frame: str | None = None,
    elapsed: int = 0,
) -> str:
    """Render task header line (e.g. '▶ [1/3] exec: ls -la running (12s) ⠋')."""
    status = task.get("status", "")
    ttype = task.get("type", "")
    detail = task.get("detail", "")
    skill_name = task.get("skill", "")

    # Pick icon based on status
    if status == "done":
        icon = _icon("ok", caps)
    elif status == "failed":
        icon = _icon("fail", caps)
    elif status == "cancelled":
        icon = _icon("cancel", caps)
    elif status == "skipped":
        icon = _icon("skip", caps)
    else:
        # running or other → type icon
        icon = _icon(ttype, caps)

    # Label: wrapper:name for wrapper tasks with a skill/wrapper field
    label = f"wrapper:{skill_name}" if ttype == "wrapper" and skill_name else ttype

    # Build detail part — first line only, to keep the header on one line
    if detail:
        first_line = detail.split("\n", 1)[0].strip()
        detail_str = f": {first_line}" if first_line else ""
    else:
        detail_str = ""
    text = f"{icon} [{index}/{total}] {label}{detail_str}"
    if status == "skipped":
        text += "  (skipped)"

    # Append substatus + spinner frame when running
    if spinner_frame is not None:
        substatus = task.get("substatus") or ""
        label_map = {
            "translating": "translating",
            "executing": "running",
            "reviewing": "reviewing",
            "searching": "searching",
            "composing": "composing",
        }
        phase_label = label_map.get(substatus, "")
        spinner = _style(spinner_frame, _CYAN, caps=caps)
        if phase_label:
            text = f"{text} {phase_label}{_format_elapsed(elapsed)} {spinner}"
        else:
            text = f"{text}{_format_elapsed(elapsed)} {spinner}"

    # Append elapsed duration for completed tasks
    elif status in ("done", "failed") and task.get("duration_ms") is not None:
        duration_s = task["duration_ms"] // 1000
        if duration_s >= 1:
            text = f"{text} ({_fmt_duration(duration_s)})"

    # Pick color based on type
    if status == "done":
        color = _GREEN
    elif status == "failed":
        color = _RED
    elif ttype == "msg":
        color = _GREEN
    else:
        color = _YELLOW

    styled = _style(text, color, caps=caps)

    # Truncate to width - 2 to avoid line-wrap artifacts with \r
    if caps.tty and len(text) > caps.width - 2:
        # Truncate the unstyled text, then re-style
        truncated = text[: caps.width - 5] + "..."
        styled = _style(truncated, color, caps=caps)

    return styled


def render_task_output(
    output: str, caps: TermCaps, *, is_msg: bool = False,
) -> str:
    """Render indented task output with optional truncation."""
    if not output:
        return ""
    indent_char = _sym("┊", "|", caps)
    indent = f"  {indent_char} "
    lines = output.splitlines()

    # Determine truncation limit
    if is_msg or not caps.tty:
        # Never truncate msg output or non-TTY
        max_lines = len(lines)
    elif caps.height < 40:
        max_lines = 10
    else:
        max_lines = 20

    shown = lines[:max_lines]
    result_lines = [_style(f"{indent}{line}", _DIM, caps=caps) for line in shown]

    if len(lines) > max_lines:
        remaining = len(lines) - max_lines
        more = f"  ... ({remaining} more lines)"
        result_lines.append(_style(more, _DIM, caps=caps))

    return "\n".join(result_lines)


def render_separator(caps: TermCaps) -> str:
    """Render a horizontal separator line."""
    char = _sym("─", "-", caps)
    width = min(caps.width, 60)
    line = char * width
    return _style(line, _DIM, caps=caps)


# Re-exported from kiso.text for backwards compatibility.
from kiso.text import extract_thinking  # noqa: F401


def render_thinking(thinking: str, caps: TermCaps) -> str:
    """Render thinking block with header and indented body."""
    icon = "🤔" if caps.unicode else "?"
    header = _style(f"{icon} Thinking...", _YELLOW, caps=caps)
    indent_char = _sym("┊", "|", caps)
    lines = thinking.splitlines()
    max_lines = 10
    shown = lines[:max_lines]
    body = "\n".join(
        _style(f"  {indent_char} {line}", _DIM, _YELLOW, caps=caps) for line in shown
    )
    if len(lines) > max_lines:
        more = _style(f"  ... ({len(lines) - max_lines} more lines)", _DIM, _YELLOW, caps=caps)
        body += "\n" + more
    return f"{header}\n{body}"


def render_msg_output(
    output: str,
    caps: TermCaps,
    bot_name: str = "Bot",
    thinking: str | None = None,
) -> str:
    """Render bot message output with markdown formatting.

    The bot label (e.g. ``Bot:``) is placed on its own line, followed by
    the response body rendered as markdown via :func:`_render_markdown`.
    This keeps the label visually separate from multi-line markdown output
    (headings, code blocks, lists, tables).

    If *thinking* is provided it is used directly; otherwise thinking
    blocks are extracted from *output* via ``<think>`` tags (fallback for
    models that embed reasoning in the response text).
    """
    if thinking is None:
        thinking, clean = extract_thinking(output)
    else:
        # Thinking already extracted upstream (call_llm strips tags before
        # storing content), so output should be clean.  Use as-is.
        clean = output
    parts: list[str] = []
    if thinking:
        parts.append(render_thinking(thinking, caps))
        parts.append("")  # blank line
    label = _style(f"{bot_name}:", _BOLD, _MAGENTA, caps=caps)
    parts.append(label)
    if clean:
        parts.append(_render_markdown(clean, caps))
    return "\n".join(parts)


def render_user_prompt(user: str, caps: TermCaps) -> str:
    """Render colored user prompt for input()."""
    return _style(f"{user}:", _BOLD, _CYAN, caps=caps)


def _format_resources(resources: dict, caps: TermCaps) -> str:
    """Format resource limits dict into a compact status line."""
    parts: list[str] = []
    mem = resources.get("memory_mb", {})
    mem_used, mem_limit = mem.get("used"), mem.get("limit")
    if mem_used is not None and mem_limit is not None:
        parts.append(f"RAM: {mem_used / 1024:.1f}/{mem_limit / 1024:.0f} GB")
    cpu = resources.get("cpu", {})
    cpu_limit = cpu.get("limit")
    if cpu_limit is not None:
        parts.append(f"CPU: {cpu_limit}")
    disk = resources.get("disk_gb", {})
    disk_used, disk_limit = disk.get("used"), disk.get("limit")
    if disk_used is not None and disk_limit is not None:
        parts.append(f"Disk: {disk_used}/{disk_limit} GB")
    if not parts:
        return ""
    dot = _sym(" · ", " | ", caps)
    return "  " + dot.join(parts)


def render_banner(
    bot_name: str, session: str, caps: TermCaps,
    version: str | None = None, resources: dict | None = None,
) -> str:
    """Render welcome banner at chat startup."""
    sep = render_separator(caps)
    kiso_label = "  Kiso 基礎" if caps.unicode else "  Kiso"
    if version:
        kiso_label = f"{kiso_label}  v{version}"
    name_line = _style(kiso_label, _BOLD, _MAGENTA, caps=caps)
    dot = _sym(" · ", " | ", caps)
    caps_text = f"  run commands{dot}search the web{dot}write code{dot}use skills"
    caps_line = _style(caps_text, _DIM, caps=caps)
    hint = _style(f"  /help for commands{dot}Ctrl+C to cancel a task", _DIM, caps=caps)
    instance_session = _style(f"  instance: {bot_name}  |  session: {session}", _DIM, caps=caps)
    lines = [sep, name_line, instance_session]
    if resources:
        res_text = _format_resources(resources, caps)
        if res_text:
            lines.append(_style(res_text, _DIM, caps=caps))
    lines.extend([caps_line, hint, sep])
    return "\n" + "\n".join(lines) + "\n"


def _fmt_duration(seconds: int) -> str:
    """Format seconds as compact duration: '0s', '5s', '1m 30s', '3m'."""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def _format_elapsed(seconds: int) -> str:
    """Format elapsed time: '' (<2s), 'for 5s', 'for 1m 30s', 'for 3m 45s'."""
    if seconds < 2:
        return ""
    return f" for {_fmt_duration(seconds)}"


_PHASE_DISPLAY_LABELS = {
    "classifying": "Classifying",
    "planning": "Planning",
    "executing": "Executing",
    "idle": "Waiting",
}

_PHASE_DONE_LABELS = {
    "classifying": "Classified",
    "planning": "Planned",
    "executing": "Executed",
    "idle": "",
}


def render_planner_spinner(
    caps: TermCaps, spinner_frame: str, elapsed: int = 0, phase: str = "planning",
) -> str:
    """Render worker phase spinner (e.g. '◆ Planning for 45s ⠋').

    *phase* is the worker phase label: "classifying", "planning", "executing", etc.
    """
    icon = _icon("plan", caps)
    frame = _style(spinner_frame, _CYAN, caps=caps)
    label = _PHASE_DISPLAY_LABELS.get(phase, phase.capitalize())
    text = f"{icon} {label}{_format_elapsed(elapsed)} {frame}"
    return _style(text, _CYAN, caps=caps)


def render_phase_done(phase: str, elapsed: float, caps: TermCaps) -> str:
    """Render phase completion line (e.g. '  ✓ Classified in 2s')."""
    label = _PHASE_DONE_LABELS.get(phase, "")
    if not label:
        return ""
    icon = _icon("ok", caps)
    text = f"  {icon} {label} in {_fmt_duration(int(elapsed))}"
    return _style(text, _DIM, _GREEN, caps=caps)


def render_cancel_start(caps: TermCaps) -> str:
    """Render cancel-in-progress message."""
    icon = _icon("cancel", caps)
    text = f"{icon} Cancelling..."
    return _style(text, _BOLD, _RED, caps=caps)


def render_step_usage(input_tokens: int, output_tokens: int, caps: TermCaps) -> str:
    """Render compact per-step token usage (e.g. '⟨430→85⟩')."""
    if not input_tokens and not output_tokens:
        return ""
    arrow = _sym("→", "->", caps)
    lp = "⟨" if caps.unicode else "<"
    rp = "⟩" if caps.unicode else ">"
    text = f"{lp}{input_tokens:,}{arrow}{output_tokens:,}{rp}"
    return _style(text, _DIM, caps=caps)


def _parse_llm_calls(llm_calls_json: str | None) -> list[dict]:
    """Parse *llm_calls_json* safely.  Returns ``[]`` on ``None``/empty/invalid."""
    if not llm_calls_json:
        return []
    import json as _json
    try:
        calls = _json.loads(llm_calls_json)
    except (ValueError, TypeError):
        return []
    return calls if isinstance(calls, list) else []


def get_last_thinking(llm_calls_json: str | None) -> str | None:
    """Return the thinking field from the last LLM call, or ``None``."""
    calls = _parse_llm_calls(llm_calls_json)
    if calls:
        return calls[-1].get("thinking") or None
    return None


def render_llm_calls(llm_calls_json: str | None, caps: TermCaps) -> str:
    """Render per-LLM-call breakdown.

    Each call shows: role, model, input→output tokens.
    Example:
      translator ⟨300→45 deepseek/deepseek-v3⟩
      reviewer   ⟨350→60 deepseek/deepseek-v3⟩
    """
    calls = _parse_llm_calls(llm_calls_json)
    if not calls:
        return ""
    lines: list[str] = []
    arrow = _sym("→", "->", caps)
    for c in calls:
        role = c.get("role", "?")
        model = c.get("model", "")
        in_t = c.get("input_tokens", 0)
        out_t = c.get("output_tokens", 0)
        dur = c.get("duration_ms")
        elapsed = f" {_fmt_duration(dur // 1000)}" if dur and dur >= 1000 else ""
        text = f"  {role:12s} {in_t:,}{arrow}{out_t:,}{elapsed}  {_short_model(model)}"
        lines.append(_style(text, _DIM, caps=caps))
    return "\n".join(lines)


def _verbose_call_chars(caps: TermCaps) -> tuple[str, str, str]:
    """Return ``(arrow, sep_char, think_sep)`` used by verbose panels."""
    arrow = _sym("\u2192", "->", caps)
    sep_char = _sym("\u2500", "-", caps)
    think_icon = _sym("\U0001f914", "?", caps)
    think_sep = _labeled_sep(f" {think_icon} reasoning ", sep_char)
    return arrow, sep_char, think_sep


def render_llm_call_input_panel(call: dict, caps: TermCaps) -> str:
    """Render only the *input* panel for a single LLM call.

    Used during live polling to show what was sent before the response arrives.
    """
    from rich.markup import escape as _esc
    from rich.panel import Panel

    messages = call.get("messages", [])
    if not messages:
        return ""

    console, buf = _make_rich_console(caps)
    arrow, _, _ = _verbose_call_chars(caps)

    role = call.get("role", "?")
    model = call.get("model", "")
    in_t = call.get("input_tokens", 0)
    sm = _short_model(model)

    if in_t:
        token_detail = f"{in_t:,} tokens"
    else:
        # Estimate from message content (~4 chars per token)
        chars = sum(len(m.get("content", "")) for m in messages)
        est = max(1, chars // 4)
        token_detail = f"~{est:,} tokens"

    title = _verbose_title(
        _esc, role, sm, arrow, call.get("ts"),
        detail=token_detail, direction="IN",
    )
    parts = _build_message_parts(messages, _esc)
    console.print(Panel(
        "\n".join(parts), title=title, border_style="dim cyan",
        title_align="left", expand=True,
    ))
    return _rich_buf_to_str(buf)


def _format_curator_output(parsed: dict, esc) -> str:
    """Format curator evaluations for readable verbose output."""
    from kiso.brain import CURATOR_VERDICT_PROMOTE, CURATOR_VERDICT_ASK, CURATOR_VERDICT_DISCARD

    lines: list[str] = []
    for ev in parsed["evaluations"]:
        verdict = ev.get("verdict", "?")
        lid = ev.get("learning_id", "?")
        if verdict == CURATOR_VERDICT_PROMOTE:
            fact = ev.get("fact", "")
            lines.append(f"[bold green]promote[/bold green] [dim]\\[id={lid}][/dim] {esc(fact)}")
            meta: list[str] = []
            entity = ev.get("entity_name")
            if entity:
                kind = ev.get("entity_kind", "")
                meta.append(f"entity: {esc(entity)} ({esc(kind)})")
            tags = ev.get("tags")
            if tags:
                meta.append(f"tags: {esc(', '.join(tags))}")
            if meta:
                lines.append(f"  [dim]{' | '.join(meta)}[/dim]")
        elif verdict == CURATOR_VERDICT_ASK:
            question = ev.get("question", "")
            lines.append(f"[bold yellow]ask[/bold yellow]     [dim]\\[id={lid}][/dim] {esc(question)}")
        elif verdict == CURATOR_VERDICT_DISCARD:
            reason = ev.get("reason", "")
            lines.append(f"[dim]discard  \\[id={lid}] {esc(reason)}[/dim]")
        else:
            lines.append(f"{esc(verdict):8s} [dim]\\[id={lid}][/dim]")
    return "\n".join(lines)


def render_llm_call_output_panel(call: dict, caps: TermCaps) -> str:
    """Render the *summary line + output panel* for a single LLM call.

    Used after the input panel was already shown, to complete the pair.
    """
    import json as _json
    from rich.markup import escape as _esc
    from rich.panel import Panel

    response = call.get("response", "")
    if not call.get("messages"):
        return ""

    console, buf = _make_rich_console(caps)
    arrow, _, think_sep = _verbose_call_chars(caps)

    role = call.get("role", "?")
    model = call.get("model", "")
    in_t = call.get("input_tokens", 0)
    out_t = call.get("output_tokens", 0)
    thinking = call.get("thinking", "")
    sm = _short_model(model)
    dur = call.get("duration_ms")
    elapsed = f", {_fmt_duration(dur // 1000)}" if dur and dur >= 1000 else ""

    # Summary line
    summary = f"  {_esc(role):12s} {in_t:,}{arrow}{out_t:,}{elapsed}  {_esc(sm)}"
    console.print(f"[dim]{summary}[/dim]")

    # Output panel
    title = _verbose_title(
        _esc, role, sm, arrow, call.get("ts"),
        detail=f"{out_t:,} tokens{elapsed}", direction="OUT",
    )
    parts: list[str] = []
    if thinking:
        parts.append(f"[bold yellow]{think_sep}[/bold yellow]")
        parts.append(f"[dim yellow]{_esc(thinking)}[/dim yellow]")
        parts.append("")
    try:
        parsed = _json.loads(response)
        if role == "curator" and isinstance(parsed.get("evaluations"), list):
            parts.append(_format_curator_output(parsed, _esc))
        else:
            parts.append(_esc(_json.dumps(parsed, indent=2, ensure_ascii=False)))
    except (ValueError, TypeError):
        parts.append(_esc(response))

    console.print(Panel(
        "\n".join(parts), title=title, border_style="dim green",
        title_align="left", expand=True,
    ))
    return _rich_buf_to_str(buf)


def render_llm_calls_verbose(
    llm_calls_json: str | None, caps: TermCaps, skip: int = 0,
) -> str:
    """Render full LLM input/output as separate Input and Output panels.

    Each call produces TWO panels:
    - **Input panel** (cyan border): messages sent to the LLM
    - **Output panel** (green border): response (+ thinking if present)

    Between the panels a compact summary line shows tokens and duration.

    *skip* omits the first N verbose calls (used by incremental rendering
    to avoid re-printing panels already shown).
    """
    calls = _parse_llm_calls(llm_calls_json)
    verbose_calls = [c for c in calls if c.get("messages")]
    if skip >= len(verbose_calls):
        return ""
    verbose_calls = verbose_calls[skip:]

    parts: list[str] = []
    for c in verbose_calls:
        in_panel = render_llm_call_input_panel(c, caps)
        if in_panel:
            parts.append(in_panel)
        out_panel = render_llm_call_output_panel(c, caps)
        if out_panel:
            parts.append(out_panel)

    return "\n".join(parts)


def render_review(task: dict, caps: TermCaps) -> str:
    """Render review verdict, optional learning, and per-call LLM usage."""
    verdict = task.get("review_verdict")
    if not verdict:
        return ""
    lines: list[str] = []
    retry_count = task.get("retry_count") or 0
    if verdict == "ok":
        ok_text = f"  {_icon('ok', caps)} review: ok"
        if retry_count > 0:
            suffix = "retry" if retry_count == 1 else "retries"
            ok_text += f" (after {retry_count} {suffix})"
        lines.append(_style(ok_text, _GREEN, caps=caps))
    elif verdict == "replan":
        reason = task.get("review_reason") or ""
        lines.append(
            _style(
                f'  {_icon("fail", caps)} review: replan — "{reason}"',
                _BOLD, _RED, caps=caps,
            )
        )
        if retry_count > 0:
            lines.append(
                _style(f"  (retried {retry_count}x before escalating)", _DIM, caps=caps)
            )
    # Per-call LLM breakdown
    llm_detail = render_llm_calls(task.get("llm_calls"), caps)
    if llm_detail:
        lines.append(llm_detail)
    learning = task.get("review_learning")
    if learning:
        prefix = "  📝 learning: " if caps.unicode else "  + learning: "
        learn_line = f'{prefix}"{learning}"'
        learning_tags = task.get("review_learning_tags")
        if learning_tags:
            tags = [t.strip() for t in learning_tags.split(",") if t.strip()]
            if tags:
                learn_line += _style(f" [{', '.join(tags)}]", _DIM, caps=caps)
        lines.append(_style(learn_line, _MAGENTA, caps=caps))
    return "\n".join(lines)


def render_inflight_indicator(call: dict, caps: TermCaps) -> str:
    """Render a compact one-line indicator for an in-flight LLM call.

    Example: ``  ⏳ reviewer → deepseek-v3.2 (waiting...)``
    """
    role = call.get("role", "?")
    model = call.get("model", "")
    arrow = _sym("\u2192", "->", caps)
    wait_icon = _sym("\u23f3", "...", caps)
    text = f"  {wait_icon} {role} {arrow} {_short_model(model)} (waiting...)"
    return _style(text, _DIM, caps=caps)


def _visible_len(text: str) -> int:
    """Return the visible length of *text*, stripping ANSI escape codes."""
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def render_partial_content(
    text: str, caps: TermCaps, max_lines: int = 6,
) -> tuple[str, int]:
    """Render a compact partial-content block from live streaming output.

    Shows the last *max_lines* lines of *text*, dimmed, with a streaming icon.

    Returns (rendered_string, visual_line_count) where visual_line_count
    accounts for terminal line wrapping.
    """
    if not text:
        return "", 0
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    icon = _sym("\u25b8", ">", caps)
    styled = [_style(f"  {icon} {line}", _DIM, caps=caps) for line in lines]
    rendered = "\n".join(styled)
    # Calculate visual lines accounting for terminal width wrapping.
    visual_lines = 0
    for s in styled:
        vlen = _visible_len(s)
        visual_lines += max(1, -(-vlen // caps.width))  # ceil division
    return rendered, visual_lines


def render_cancel_done(
    done: int,
    total: int,
    done_tasks: list[str],
    skipped_tasks: list[str],
    caps: TermCaps,
) -> str:
    """Render cancel summary."""
    icon = _icon("cancel", caps)
    header = f"{icon} Cancelled. {done} of {total} tasks completed."
    parts = [_style(header, _BOLD, _RED, caps=caps)]
    if done_tasks:
        parts.append(_style("  Done: " + ", ".join(done_tasks), _DIM, caps=caps))
    if skipped_tasks:
        parts.append(_style("  Skipped: " + ", ".join(skipped_tasks), _DIM, caps=caps))
    return "\n".join(parts)
