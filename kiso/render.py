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


# ── Icons ────────────────────────────────────────────────────

_ICONS_UNICODE = {
    "plan": "◆",
    "exec": "▶",
    "skill": "⚡",
    "msg": "💬",
    "ok": "✓",
    "fail": "✗",
    "replan": "↻",
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

    # Build detail part
    detail_str = f": {detail}" if detail else ""
    text = f"{icon} [{index}/{total}] {label}{detail_str}"

    # Append spinner frame
    if spinner_frame is not None:
        spinner = _style(spinner_frame, _CYAN, caps=caps)
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
    """Render bot message output."""
    thinking, clean = extract_thinking(output)
    parts: list[str] = []
    if thinking:
        parts.append(render_thinking(thinking, caps))
        parts.append("")  # blank line
    label = _style(f"{bot_name}:", _BOLD, _MAGENTA, caps=caps)
    parts.append(f"{label} {clean}")
    return "\n".join(parts)


def render_user_prompt(user: str, caps: TermCaps) -> str:
    """Render colored user prompt for input()."""
    return _style(f"{user}:", _BOLD, _CYAN, caps=caps)


def render_banner(bot_name: str, session: str, caps: TermCaps) -> str:
    """Render welcome banner at chat startup."""
    sep = render_separator(caps)
    display_name = f"  基礎 {bot_name}" if caps.unicode and bot_name == "Kiso" else f"  {bot_name}"
    name_line = _style(display_name, _BOLD, _MAGENTA, caps=caps)
    session_line = _style(f"  session: {session}", _DIM, caps=caps)
    hint = _style("  Type a message. /help for commands.", _DIM, caps=caps)
    return f"\n{sep}\n{name_line}\n{session_line}\n{hint}\n{sep}\n"


def render_cancel_start(caps: TermCaps) -> str:
    """Render cancel-in-progress message."""
    icon = _icon("cancel", caps)
    text = f"{icon} Cancelling..."
    return _style(text, _BOLD, _RED, caps=caps)


def render_review(task: dict, caps: TermCaps) -> str:
    """Render review verdict and optional learning for a task."""
    verdict = task.get("review_verdict")
    if not verdict:
        return ""
    lines: list[str] = []
    if verdict == "ok":
        lines.append(_style(f"  {_icon('ok', caps)} review: ok", _GREEN, caps=caps))
    elif verdict == "replan":
        reason = task.get("review_reason") or ""
        lines.append(_style(
            f'  {_icon("fail", caps)} review: replan — "{reason}"',
            _BOLD, _RED, caps=caps,
        ))
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
