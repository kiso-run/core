"""Terminal renderer for CLI display."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass


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
    from rich.console import Console
    from rich.markdown import Markdown

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
    md = Markdown(text, code_theme="monokai")
    console.print(md)
    raw = buf.getvalue()
    # Strip trailing whitespace per line (rich pads to full width)
    lines = [line.rstrip() for line in raw.splitlines()]
    # Remove trailing empty lines
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


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
    s = "s" if task_count != 1 else ""
    text = f"{icon} {label}: {goal} ({task_count} task{s})"
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
    for i, t in enumerate(tasks, 1):
        ttype = t.get("type", "?")
        detail = (t.get("detail") or "").split("\n", 1)[0].strip()
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
    """Render token usage summary.

    Example: ⟨ 1,234 in → 567 out │ deepseek/deepseek-v3.2 ⟩
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
    if model:
        sep = "│" if caps.unicode else "|"
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
) -> str:
    """Render task header line (e.g. '▶ [1/3] exec: ls -la ⠋')."""
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
    else:
        # running or other → type icon
        icon = _icon(ttype, caps)

    # Label: skill:name for skill tasks with a skill field
    label = f"skill:{skill_name}" if ttype == "skill" and skill_name else ttype

    # Build detail part — first line only, to keep the header on one line
    if detail:
        first_line = detail.split("\n", 1)[0].strip()
        detail_str = f": {first_line}" if first_line else ""
    else:
        detail_str = ""
    text = f"{icon} [{index}/{total}] {label}{detail_str}"

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
            text = f"{text} {phase_label} {spinner}"
        else:
            text = f"{text} {spinner}"

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
    indent_char = "┊" if caps.unicode else "|"
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
    char = "─" if caps.unicode else "-"
    width = min(caps.width, 60)
    line = char * width
    return _style(line, _DIM, caps=caps)


_THINK_RE = re.compile(
    r"<think(?:ing)?>(.*?)</think(?:ing)?>",
    re.DOTALL,
)


def extract_thinking(text: str) -> tuple[str, str]:
    """Extract thinking blocks from text. Returns (thinking, clean_text)."""
    blocks = []
    for m in _THINK_RE.finditer(text):
        blocks.append(m.group(1).strip())
    clean = _THINK_RE.sub("", text).strip()
    return "\n".join(blocks), clean


def render_thinking(thinking: str, caps: TermCaps) -> str:
    """Render thinking block with header and indented body."""
    icon = "🤔" if caps.unicode else "?"
    header = _style(f"{icon} Thinking...", _YELLOW, caps=caps)
    indent_char = "┊" if caps.unicode else "|"
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


def render_msg_output(output: str, caps: TermCaps, bot_name: str = "Bot") -> str:
    """Render bot message output with markdown formatting.

    The bot label (e.g. ``Bot:``) is placed on its own line, followed by
    the response body rendered as markdown via :func:`_render_markdown`.
    This keeps the label visually separate from multi-line markdown output
    (headings, code blocks, lists, tables).
    """
    thinking, clean = extract_thinking(output)
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


def render_banner(bot_name: str, session: str, caps: TermCaps, version: str | None = None) -> str:
    """Render welcome banner at chat startup."""
    sep = render_separator(caps)
    kiso_label = "  Kiso 基礎" if caps.unicode else "  Kiso"
    if version:
        kiso_label = f"{kiso_label}  v{version}"
    name_line = _style(kiso_label, _BOLD, _MAGENTA, caps=caps)
    dot = " · " if caps.unicode else " | "
    caps_text = f"  run commands{dot}search the web{dot}write code{dot}use skills"
    caps_line = _style(caps_text, _DIM, caps=caps)
    hint = _style(f"  /help for commands{dot}Ctrl+C to cancel a task", _DIM, caps=caps)
    instance_session = _style(f"  instance: {bot_name}  |  session: {session}", _DIM, caps=caps)
    lines = [sep, name_line, instance_session, caps_line, hint, sep]
    return "\n" + "\n".join(lines) + "\n"


def render_planner_spinner(caps: TermCaps, spinner_frame: str) -> str:
    """Render planner phase spinner (e.g. '◆ Planning... ⠋')."""
    icon = _icon("plan", caps)
    frame = _style(spinner_frame, _CYAN, caps=caps)
    text = f"{icon} Planning... {frame}"
    return _style(text, _CYAN, caps=caps)


def render_cancel_start(caps: TermCaps) -> str:
    """Render cancel-in-progress message."""
    icon = _icon("cancel", caps)
    text = f"{icon} Cancelling..."
    return _style(text, _BOLD, _RED, caps=caps)


def render_step_usage(input_tokens: int, output_tokens: int, caps: TermCaps) -> str:
    """Render compact per-step token usage (e.g. '⟨430→85⟩')."""
    if not input_tokens and not output_tokens:
        return ""
    arrow = "→" if caps.unicode else "->"
    lp = "⟨" if caps.unicode else "<"
    rp = "⟩" if caps.unicode else ">"
    text = f"{lp}{input_tokens:,}{arrow}{output_tokens:,}{rp}"
    return _style(text, _DIM, caps=caps)


def render_llm_calls(llm_calls_json: str | None, caps: TermCaps) -> str:
    """Render per-LLM-call breakdown.

    Each call shows: role, model, input→output tokens.
    Example:
      translator ⟨300→45 deepseek/deepseek-v3⟩
      reviewer   ⟨350→60 deepseek/deepseek-v3⟩
    """
    if not llm_calls_json:
        return ""
    import json as _json
    try:
        calls = _json.loads(llm_calls_json)
    except (ValueError, TypeError):
        return ""
    if not calls:
        return ""
    lines: list[str] = []
    arrow = "→" if caps.unicode else "->"
    for c in calls:
        role = c.get("role", "?")
        model = c.get("model", "")
        in_t = c.get("input_tokens", 0)
        out_t = c.get("output_tokens", 0)
        # Shorten model name: "deepseek/deepseek-chat-v3" → "deepseek-chat-v3"
        short_model = model.split("/", 1)[-1] if "/" in model else model
        text = f"  {role:12s} {in_t:,}{arrow}{out_t:,}  {short_model}"
        lines.append(_style(text, _DIM, caps=caps))
    return "\n".join(lines)


def render_llm_calls_verbose(llm_calls_json: str | None, caps: TermCaps) -> str:
    """Render full LLM input/output with beautified JSON in bordered panels.

    Each call is shown as a rich Panel with:
    - Title: role -> model (tokens)
    - Body: each message (role-labeled), then the response
    - JSON responses are pretty-printed with syntax highlighting
    """
    if not llm_calls_json:
        return ""
    import json as _json
    try:
        calls = _json.loads(llm_calls_json)
    except (ValueError, TypeError):
        return ""
    if not calls:
        return ""

    # Only render calls that have messages (verbose data present)
    verbose_calls = [c for c in calls if c.get("messages")]
    if not verbose_calls:
        return ""

    from rich.console import Console
    from rich.panel import Panel

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

    for c in verbose_calls:
        role = c.get("role", "?")
        model = c.get("model", "")
        in_t = c.get("input_tokens", 0)
        out_t = c.get("output_tokens", 0)
        messages = c.get("messages", [])
        response = c.get("response", "")

        short_model = model.split("/", 1)[-1] if "/" in model else model
        arrow = "\u2192" if caps.unicode else "->"
        title = f" {role} {arrow} {short_model} ({in_t:,}{arrow}{out_t:,}) "

        # Build body
        parts: list[str] = []
        for msg in messages:
            label = msg.get("role", "?")
            content = msg.get("content", "")
            parts.append(f"[{label}]")
            parts.append(content)
            parts.append("")

        parts.append("[response]")
        # Try to beautify JSON responses
        try:
            parsed = _json.loads(response)
            parts.append(_json.dumps(parsed, indent=2, ensure_ascii=False))
        except (ValueError, TypeError):
            parts.append(response)

        body = "\n".join(parts)
        panel = Panel(
            body,
            title=title,
            border_style="dim magenta",
            title_align="left",
            expand=True,
        )
        console.print(panel)

    raw = buf.getvalue()
    lines = [line.rstrip() for line in raw.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


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
        lines.append(_style(f'{prefix}"{learning}"', _MAGENTA, caps=caps))
    return "\n".join(lines)


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
