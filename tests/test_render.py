"""Tests for kiso.render — terminal capability detection and render functions."""

from __future__ import annotations

import pytest

from kiso.render import (
    CLEAR_LINE,
    TermCaps,
    _icon,
    _style,
    detect_caps,
    extract_thinking,
    render_banner,
    render_cancel_done,
    render_cancel_start,
    render_max_replan,
    render_msg_output,
    render_plan,
    render_review,
    render_separator,
    render_task_header,
    render_task_output,
    render_thinking,
    render_user_prompt,
    spinner_frames,
)

# ── Helpers ──────────────────────────────────────────────────


def _caps(
    *,
    color: bool = True,
    unicode: bool = True,
    width: int = 120,
    height: int = 50,
    tty: bool = True,
) -> TermCaps:
    return TermCaps(color=color, unicode=unicode, width=width, height=height, tty=tty)


_COLOR = _caps(color=True, unicode=True)
_PLAIN = _caps(color=False, unicode=False, tty=False)


# ── detect_caps ──────────────────────────────────────────────


def test_detect_caps_tty_256color(monkeypatch):
    monkeypatch.setattr("sys.stdout", type("FakeTTY", (), {"isatty": lambda self: True})())
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("COLORTERM", raising=False)
    caps = detect_caps()
    assert caps.color is True
    assert caps.unicode is True
    assert caps.tty is True


def test_detect_caps_no_tty(monkeypatch):
    monkeypatch.setattr("sys.stdout", type("FakeNoTTY", (), {"isatty": lambda self: False})())
    monkeypatch.setenv("TERM", "xterm-256color")
    caps = detect_caps()
    assert caps.color is False
    assert caps.tty is False


def test_detect_caps_colorterm(monkeypatch):
    monkeypatch.setattr("sys.stdout", type("FakeTTY", (), {"isatty": lambda self: True})())
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("COLORTERM", "truecolor")
    caps = detect_caps()
    assert caps.color is True


def test_detect_caps_no_unicode(monkeypatch):
    monkeypatch.setattr("sys.stdout", type("FakeTTY", (), {"isatty": lambda self: True})())
    monkeypatch.setenv("LANG", "C")
    monkeypatch.delenv("LC_ALL", raising=False)
    caps = detect_caps()
    assert caps.unicode is False


def test_detect_caps_width_fallback(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("no terminal")

    monkeypatch.setattr("kiso.render.os.get_terminal_size", _raise)
    caps = detect_caps()
    assert caps.width == 80
    assert caps.height == 24


# ── Icons ────────────────────────────────────────────────────


def test_icons_unicode():
    caps = _caps(unicode=True)
    assert _icon("plan", caps) == "◆"
    assert _icon("exec", caps) == "▶"
    assert _icon("ok", caps) == "✓"
    assert _icon("fail", caps) == "✗"


def test_icons_ascii():
    caps = _caps(unicode=False)
    assert _icon("plan", caps) == "*"
    assert _icon("exec", caps) == ">"
    assert _icon("ok", caps) == "ok"
    assert _icon("fail", caps) == "FAIL"


# ── _style ───────────────────────────────────────────────────


def test_style_with_color():
    caps = _caps(color=True)
    result = _style("hello", "\033[32m", caps=caps)
    assert "\033[32m" in result
    assert "\033[0m" in result
    assert "hello" in result


def test_style_without_color():
    caps = _caps(color=False)
    result = _style("hello", "\033[32m", caps=caps)
    assert result == "hello"


# ── render_plan ──────────────────────────────────────────────


def test_render_plan_normal():
    result = render_plan("Do stuff", 3, _COLOR)
    assert "Plan: Do stuff" in result
    assert "3 tasks" in result
    assert "◆" in result


def test_render_plan_singular():
    result = render_plan("One thing", 1, _COLOR)
    assert "1 task)" in result
    assert "tasks" not in result


def test_render_plan_replan():
    result = render_plan("Try again", 2, _COLOR, replan=True)
    assert "Replan: Try again" in result
    assert "↻" in result


def test_render_plan_ascii():
    result = render_plan("Do stuff", 2, _PLAIN)
    assert "Plan: Do stuff" in result
    assert "*" in result
    assert "\033[" not in result


# ── render_max_replan ────────────────────────────────────────


def test_render_max_replan():
    result = render_max_replan(3, _COLOR)
    assert "Max replans reached (3)" in result
    assert "Giving up" in result


def test_render_max_replan_no_color():
    result = render_max_replan(5, _PLAIN)
    assert "5" in result
    assert "\033[" not in result


# ── render_task_header ───────────────────────────────────────


def test_render_task_header_running():
    task = {"type": "exec", "detail": "ls -la", "status": "running"}
    result = render_task_header(task, 1, 3, _COLOR)
    assert "[1/3]" in result
    assert "exec: ls -la" in result
    assert "▶" in result


def test_render_task_header_done():
    task = {"type": "exec", "detail": "ls", "status": "done"}
    result = render_task_header(task, 2, 3, _COLOR)
    assert "✓" in result
    assert "exec: ls" in result


def test_render_task_header_failed():
    task = {"type": "exec", "detail": "bad", "status": "failed"}
    result = render_task_header(task, 1, 2, _COLOR)
    assert "✗" in result
    assert "exec: bad" in result


def test_render_task_header_skill_with_name():
    task = {"type": "skill", "detail": "search query", "status": "running", "skill": "web_search"}
    result = render_task_header(task, 1, 2, _COLOR)
    assert "skill:web_search" in result


def test_render_task_header_with_spinner():
    task = {"type": "exec", "detail": "ls", "status": "running"}
    result = render_task_header(task, 1, 2, _COLOR, spinner_frame="⠋")
    assert "⠋" in result


def test_render_task_header_plain():
    task = {"type": "exec", "detail": "ls", "status": "running"}
    result = render_task_header(task, 1, 2, _PLAIN)
    assert "[1/2]" in result
    assert "exec: ls" in result
    assert "\033[" not in result


# ── render_task_output ───────────────────────────────────────


def test_render_task_output_basic():
    result = render_task_output("line1\nline2\nline3", _COLOR)
    assert "┊" in result
    assert "line1" in result
    assert "line3" in result


def test_render_task_output_truncation_20():
    lines = "\n".join(f"line{i}" for i in range(30))
    caps = _caps(height=50, tty=True)
    result = render_task_output(lines, caps)
    assert "line19" in result
    assert "line20" not in result
    assert "10 more lines" in result


def test_render_task_output_truncation_10_short_terminal():
    lines = "\n".join(f"line{i}" for i in range(20))
    caps = _caps(height=30, tty=True)
    result = render_task_output(lines, caps)
    assert "line9" in result
    assert "line10" not in result
    assert "10 more lines" in result


def test_render_task_output_no_truncation_msg():
    lines = "\n".join(f"line{i}" for i in range(30))
    caps = _caps(height=50, tty=True)
    result = render_task_output(lines, caps, is_msg=True)
    assert "line29" in result
    assert "more lines" not in result


def test_render_task_output_no_truncation_non_tty():
    lines = "\n".join(f"line{i}" for i in range(30))
    caps = _caps(tty=False)
    result = render_task_output(lines, caps)
    assert "line29" in result
    assert "more lines" not in result


def test_render_task_output_indent_ascii():
    result = render_task_output("hello", _PLAIN)
    assert "|" in result
    assert "hello" in result


def test_render_task_output_empty():
    result = render_task_output("", _COLOR)
    assert result == ""


# ── extract_thinking ─────────────────────────────────────────


def test_extract_thinking_basic():
    thinking, rest = extract_thinking("<think>hello</think>rest")
    assert thinking == "hello"
    assert rest == "rest"


def test_extract_thinking_none():
    text = "just plain text"
    thinking, rest = extract_thinking(text)
    assert thinking == ""
    assert rest == text


def test_extract_thinking_multiline():
    text = "<think>line1\nline2\nline3</think>answer"
    thinking, rest = extract_thinking(text)
    assert "line1" in thinking
    assert "line2" in thinking
    assert "line3" in thinking
    assert rest == "answer"


def test_extract_thinking_alt_tag():
    thinking, rest = extract_thinking("<thinking>deep thought</thinking>result")
    assert thinking == "deep thought"
    assert rest == "result"


# ── render_thinking ──────────────────────────────────────────


def test_render_thinking_basic():
    result = render_thinking("I should greet the user", _COLOR)
    assert "Thinking..." in result
    assert "🤔" in result
    assert "┊" in result
    assert "I should greet the user" in result
    # Header is yellow, body is dim yellow
    assert "\033[33m" in result  # yellow


def test_render_thinking_truncation():
    lines = "\n".join(f"thought {i}" for i in range(15))
    result = render_thinking(lines, _COLOR)
    assert "thought 9" in result
    assert "thought 10" not in result
    assert "5 more lines" in result


# ── render_separator ─────────────────────────────────────────


def test_render_separator_unicode():
    result = render_separator(_COLOR)
    assert "─" in result


def test_render_separator_ascii():
    result = render_separator(_PLAIN)
    assert "-" in result
    assert "\033[" not in result


# ── render_msg_output ────────────────────────────────────────


def test_render_msg_output_with_thinking():
    result = render_msg_output("<think>hmm</think>Hello!", _COLOR, "Kiso")
    assert "Kiso:" in result
    assert "Hello!" in result
    assert "Thinking..." in result
    assert "hmm" in result


def test_render_msg_output_no_thinking():
    result = render_msg_output("Hello there!", _COLOR, "Kiso")
    assert "Kiso:" in result
    assert "Hello there!" in result
    assert "Thinking" not in result
    assert "\033[35m" in result  # magenta


def test_render_msg_output():
    result = render_msg_output("Hello there!", _PLAIN)
    assert "Bot: Hello there!" in result


def test_render_msg_output_custom_name():
    result = render_msg_output("Hi!", _PLAIN, "Jarvis")
    assert "Jarvis: Hi!" in result


# ── render_user_prompt ───────────────────────────────────────


def test_render_user_prompt_color():
    result = render_user_prompt("alice", _COLOR)
    assert "alice:" in result
    assert "\033[36m" in result  # cyan


def test_render_user_prompt_plain():
    result = render_user_prompt("alice", _PLAIN)
    assert result == "alice:"


# ── render_banner ────────────────────────────────────────────


def test_render_banner_color():
    result = render_banner("Kiso", "host@alice", _COLOR)
    assert "Kiso" in result
    assert "host@alice" in result
    assert "exit" in result
    assert "\033[35m" in result  # magenta for bot name


def test_render_banner_plain():
    result = render_banner("Kiso", "host@alice", _PLAIN)
    assert "Kiso" in result
    assert "host@alice" in result
    assert "\033[" not in result


# ── render_cancel_start / done ───────────────────────────────


def test_render_cancel_start():
    result = render_cancel_start(_COLOR)
    assert "Cancelling" in result
    assert "⊘" in result


def test_render_cancel_start_plain():
    result = render_cancel_start(_PLAIN)
    assert "Cancelling" in result
    assert "\033[" not in result


def test_render_cancel_done():
    result = render_cancel_done(2, 5, ["task1", "task2"], ["task3", "task4", "task5"], _COLOR)
    assert "2 of 5" in result
    assert "Cancelled" in result
    assert "task1" in result
    assert "task3" in result


def test_render_cancel_done_no_skipped():
    result = render_cancel_done(3, 3, ["a", "b", "c"], [], _COLOR)
    assert "3 of 3" in result
    assert "Skipped" not in result


# ── spinner_frames ───────────────────────────────────────────


def test_spinner_frames_unicode():
    frames = spinner_frames(_caps(unicode=True))
    assert "⠋" in frames
    assert len(frames) == 10


def test_spinner_frames_ascii():
    frames = spinner_frames(_caps(unicode=False))
    assert "|" in frames
    assert len(frames) == 4


# ── Color assertions ─────────────────────────────────────────


def test_color_present_when_enabled():
    result = render_plan("Goal", 2, _COLOR)
    assert "\033[" in result


def test_color_absent_when_disabled():
    result = render_plan("Goal", 2, _PLAIN)
    assert "\033[" not in result


# ── render_review ────────────────────────────────────────────


def test_render_review_ok_unicode():
    task = {"review_verdict": "ok"}
    result = render_review(task, _COLOR)
    assert "✓" in result
    assert "review: ok" in result
    assert "\033[32m" in result  # green


def test_render_review_ok_ascii():
    task = {"review_verdict": "ok"}
    result = render_review(task, _PLAIN)
    assert "ok" in result
    assert "review: ok" in result
    assert "\033[" not in result


def test_render_review_replan_unicode():
    task = {"review_verdict": "replan", "review_reason": "Directory missing"}
    result = render_review(task, _COLOR)
    assert "✗" in result
    assert "replan" in result
    assert "Directory missing" in result
    assert "\033[31m" in result  # red


def test_render_review_replan_ascii():
    task = {"review_verdict": "replan", "review_reason": "Directory missing"}
    result = render_review(task, _PLAIN)
    assert "FAIL" in result
    assert "replan" in result
    assert "Directory missing" in result
    assert "\033[" not in result


def test_render_review_with_learning_unicode():
    task = {"review_verdict": "ok", "review_learning": "Uses pytest"}
    result = render_review(task, _COLOR)
    assert "📝" in result
    assert "learning:" in result
    assert "Uses pytest" in result
    assert "\033[35m" in result  # magenta


def test_render_review_learning_ascii():
    task = {"review_verdict": "ok", "review_learning": "Uses pytest"}
    result = render_review(task, _PLAIN)
    assert "+ learning:" in result
    assert "Uses pytest" in result
    assert "\033[" not in result


def test_render_review_no_verdict():
    task = {"review_verdict": None}
    assert render_review(task, _COLOR) == ""
    assert render_review({}, _COLOR) == ""


def test_render_review_ok_with_learning():
    task = {"review_verdict": "ok", "review_learning": "Uses Flask"}
    result = render_review(task, _COLOR)
    lines = result.split("\n")
    assert len(lines) == 2
    assert "review: ok" in lines[0]
    assert "learning:" in lines[1]
    assert "Uses Flask" in lines[1]
