"""Tests for kiso.render — terminal capability detection and render functions."""

from __future__ import annotations

import pytest

from cli.render import (
    CLEAR_LINE,
    TermCaps,
    _icon,
    _parse_llm_calls,
    _render_markdown,
    _style,
    detect_caps,
    _format_resources,
    extract_thinking,
    get_last_thinking,
    render_banner,
    render_cancel_done,
    render_cancel_start,
    render_command,
    render_llm_call_input_panel,
    render_llm_call_output_panel,
    render_llm_calls,
    render_llm_calls_verbose,
    render_max_replan,
    render_msg_output,
    render_plan,
    render_plan_detail,
    render_planner_spinner,
    render_review,
    render_separator,
    render_step_usage,
    render_task_header,
    render_task_output,
    render_thinking,
    render_usage,
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

    monkeypatch.setattr("cli.render.os.get_terminal_size", _raise)
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
    assert "Bot:" in result
    assert "Hello there!" in result


def test_render_msg_output_custom_name():
    result = render_msg_output("Hi!", _PLAIN, "Jarvis")
    assert "Jarvis:" in result
    assert "Hi!" in result


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
    assert "Kiso 基礎" in result
    assert "host@alice" in result
    assert "/help" in result
    assert "\033[35m" in result  # magenta for bot name


def test_render_banner_plain():
    result = render_banner("Kiso", "host@alice", _PLAIN)
    assert "Kiso" in result
    assert "基礎" not in result
    assert "host@alice" in result
    assert "/help" in result
    assert "\033[" not in result


def test_render_banner_custom_name_instance_line():
    result = render_banner("Jarvis", "host@alice", _COLOR)
    assert "instance: Jarvis" in result
    assert "host@alice" in result


def test_render_banner_with_resources():
    """M219: banner shows resource line when resources dict provided."""
    resources = {
        "memory_mb": {"used": 312, "limit": 4096},
        "cpu": {"limit": 2},
        "disk_gb": {"used": 3.2, "limit": 32},
        "pids": {"used": 45, "limit": 512},
    }
    result = render_banner("Kiso", "host@alice", _COLOR, resources=resources)
    assert "RAM:" in result
    assert "CPU: 2" in result
    assert "Disk: 3.2/32 GB" in result


def test_render_banner_no_resources():
    """M219: banner without resources still works (backward compatible)."""
    result = render_banner("Kiso", "host@alice", _COLOR)
    assert "RAM:" not in result
    assert "Kiso" in result


def test_render_banner_resources_none_values():
    """M219: resources with None values are skipped gracefully."""
    resources = {
        "memory_mb": {"used": None, "limit": None},
        "cpu": {"limit": None},
        "disk_gb": {"used": 3.2, "limit": 32},
        "pids": {"used": None, "limit": None},
    }
    result = render_banner("Kiso", "host@alice", _COLOR, resources=resources)
    assert "Disk: 3.2/32 GB" in result
    assert "RAM:" not in result


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


# ── render_planner_spinner ───────────────────────────────────


def test_render_planner_spinner_unicode():
    result = render_planner_spinner(_COLOR, "⠋")
    assert "Planning" in result
    assert "◆" in result
    assert "⠋" in result


def test_render_planner_spinner_ascii():
    result = render_planner_spinner(_PLAIN, "|")
    assert "Planning" in result
    assert "*" in result
    assert "\033[" not in result


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


# ── render_task_header width truncation ─────────────────────


def test_render_task_header_truncates_on_narrow_tty():
    caps = _caps(width=40, tty=True)
    task = {"type": "exec", "detail": "a" * 60, "status": "running"}
    result = render_task_header(task, 1, 2, caps)
    assert "..." in result
    # Unstyled text should have been truncated before restyling
    assert len(result.replace("\033[33m", "").replace("\033[0m", "")) <= 40


def test_render_task_header_no_truncation_when_not_tty():
    caps = _caps(width=40, tty=False)
    task = {"type": "exec", "detail": "a" * 60, "status": "running"}
    result = render_task_header(task, 1, 2, caps)
    assert "..." not in result
    assert "a" * 60 in result


def test_render_task_header_cancelled_status():
    task = {"type": "exec", "detail": "ls", "status": "cancelled"}
    result = render_task_header(task, 1, 2, _COLOR)
    assert "⊘" in result


def test_render_task_header_multiline_detail_uses_first_line():
    detail = "cat > file.md << 'EOF'\n# Title\nBody text\nEOF"
    task = {"type": "exec", "detail": detail, "status": "running"}
    result = render_task_header(task, 1, 2, _PLAIN)
    assert "cat > file.md" in result
    assert "# Title" not in result
    assert "\n" not in result


def test_render_task_header_empty_detail():
    task = {"type": "exec", "detail": "", "status": "running"}
    result = render_task_header(task, 1, 1, _PLAIN)
    assert "[1/1]" in result
    assert "exec" in result
    # No trailing colon without detail
    assert "exec:" not in result


# ── render_thinking edge cases ──────────────────────────────


def test_render_thinking_plain():
    result = render_thinking("just a thought", _PLAIN)
    assert "?" in result  # ASCII icon
    assert "Thinking..." in result
    assert "just a thought" in result
    assert "\033[" not in result


# ── render_msg_output edge cases ────────────────────────────


def test_render_msg_output_empty_string():
    result = render_msg_output("", _COLOR)
    assert "Bot:" in result or ":" in result


def test_render_msg_output_thinking_only():
    result = render_msg_output("<think>pondering</think>", _COLOR)
    assert "Thinking..." in result
    assert "pondering" in result


# ── extract_thinking edge cases ─────────────────────────────


def test_extract_thinking_multiple_blocks():
    text = "<think>first</think>middle<think>second</think>end"
    thinking, rest = extract_thinking(text)
    assert "first" in thinking
    assert "second" in thinking
    assert rest == "middleend"


def test_extract_thinking_empty_tag():
    thinking, rest = extract_thinking("<think></think>hello")
    assert thinking == ""
    assert rest == "hello"


# ── render_separator edge cases ─────────────────────────────


def test_render_separator_respects_max_width():
    caps = _caps(width=200)
    result = render_separator(caps)
    # strip ANSI to count actual chars
    plain = result.replace("\033[2m", "").replace("\033[0m", "")
    assert len(plain) == 60  # capped at 60


def test_render_separator_narrow_terminal():
    caps = _caps(width=30)
    result = render_separator(caps)
    plain = result.replace("\033[2m", "").replace("\033[0m", "")
    assert len(plain) == 30


# ── render_plan_detail ──────────────────────────────────────


def test_render_plan_detail_mixed_types():
    tasks = [
        {"type": "exec", "detail": "List files in the directory"},
        {"type": "exec", "detail": "Run uv sync"},
        {"type": "msg", "detail": "Summarize results"},
    ]
    result = render_plan_detail(tasks, _COLOR)
    assert "1." in result
    assert "2." in result
    assert "3." in result
    assert "[exec]" in result
    assert "[msg]" in result
    assert "List files" in result
    assert "Summarize results" in result


def test_render_plan_detail_empty():
    assert render_plan_detail([], _COLOR) == ""


def test_render_plan_detail_skill_type():
    tasks = [
        {"type": "skill", "detail": "Search the web"},
        {"type": "msg", "detail": "Report findings"},
    ]
    result = render_plan_detail(tasks, _PLAIN)
    assert "[skill]" in result
    assert "[msg]" in result
    assert "\033[" not in result


def test_render_plan_detail_multiline_detail():
    tasks = [{"type": "exec", "detail": "first line\nsecond line\nthird"}]
    result = render_plan_detail(tasks, _PLAIN)
    assert "first line" in result
    assert "second line" not in result


# ── render_command ──────────────────────────────────────────


def test_render_command_basic():
    result = render_command("ls -la", _COLOR)
    assert "$ ls -la" in result
    assert "\033[" in result


def test_render_command_plain():
    result = render_command("ls -la", _PLAIN)
    assert "$ ls -la" in result
    assert "\033[" not in result


# ── render_usage ────────────────────────────────────────────


def test_render_usage_with_model():
    plan = {"total_input_tokens": 1234, "total_output_tokens": 567, "model": "deepseek/deepseek-v3.2"}
    result = render_usage(plan, _COLOR)
    assert "1,234" in result
    assert "567" in result
    assert "deepseek/deepseek-v3.2" in result
    assert "⟨" in result
    assert "⟩" in result


def test_render_usage_ascii():
    plan = {"total_input_tokens": 100, "total_output_tokens": 50, "model": "gpt-4"}
    result = render_usage(plan, _PLAIN)
    assert "100" in result
    assert "50" in result
    assert "gpt-4" in result
    assert "<" in result
    assert ">" in result
    assert "\033[" not in result


def test_render_usage_no_tokens():
    plan = {"total_input_tokens": 0, "total_output_tokens": 0, "model": "gpt-4"}
    assert render_usage(plan, _COLOR) == ""


def test_render_usage_no_model():
    plan = {"total_input_tokens": 100, "total_output_tokens": 50}
    result = render_usage(plan, _COLOR)
    assert "100" in result
    assert "│" not in result


def test_render_usage_missing_keys():
    plan = {}
    assert render_usage(plan, _COLOR) == ""


# ── render_step_usage ────────────────────────────────────────


def test_render_step_usage_unicode():
    result = render_step_usage(430, 85, _COLOR)
    assert "430" in result
    assert "85" in result
    assert "\u27E8" in result  # left angle bracket
    assert "\u27E9" in result  # right angle bracket
    assert "\u2192" in result  # right arrow


def test_render_step_usage_ascii():
    result = render_step_usage(430, 85, _PLAIN)
    assert "430" in result
    assert "85" in result
    assert "<" in result
    assert ">" in result
    assert "->" in result
    assert "\033[" not in result


def test_render_step_usage_zero():
    """Returns empty string when both input and output are 0."""
    assert render_step_usage(0, 0, _COLOR) == ""
    assert render_step_usage(0, 0, _PLAIN) == ""


# ── render_review with token usage ──────────────────────────


def test_render_review_with_llm_calls():
    """Review line includes per-call LLM breakdown when llm_calls is present."""
    import json
    calls = [
        {"role": "translator", "model": "deepseek/deepseek-v3", "input_tokens": 300, "output_tokens": 45},
        {"role": "reviewer", "model": "deepseek/deepseek-v3", "input_tokens": 350, "output_tokens": 60},
    ]
    task = {"review_verdict": "ok", "llm_calls": json.dumps(calls)}
    result = render_review(task, _COLOR)
    assert "review: ok" in result
    assert "translator" in result
    assert "reviewer" in result
    assert "300" in result
    assert "45" in result


def test_render_review_without_llm_calls():
    """Review line has no LLM breakdown when llm_calls is absent."""
    task = {"review_verdict": "ok"}
    result = render_review(task, _COLOR)
    assert "review: ok" in result
    assert "translator" not in result
    assert "reviewer" not in result


# ── render_llm_calls ────────────────────────────────────────


def test_render_llm_calls_basic():
    """Renders per-call breakdown with role, tokens, and shortened model."""
    import json
    calls = [
        {"role": "planner", "model": "deepseek/deepseek-v3", "input_tokens": 400, "output_tokens": 80},
        {"role": "translator", "model": "deepseek/deepseek-v3", "input_tokens": 300, "output_tokens": 45},
        {"role": "reviewer", "model": "deepseek/deepseek-v3", "input_tokens": 350, "output_tokens": 60},
    ]
    result = render_llm_calls(json.dumps(calls), _COLOR)
    assert "planner" in result
    assert "translator" in result
    assert "reviewer" in result
    assert "400" in result
    assert "80" in result
    # Model name should be shortened (no provider prefix)
    assert "deepseek-v3" in result


def test_render_llm_calls_empty():
    """Returns empty string for None or empty input."""
    assert render_llm_calls(None, _COLOR) == ""
    assert render_llm_calls("", _COLOR) == ""
    assert render_llm_calls("[]", _COLOR) == ""


def test_render_llm_calls_ascii():
    """Uses ASCII arrow and no ANSI codes in plain mode."""
    import json
    calls = [{"role": "planner", "model": "gpt-4", "input_tokens": 100, "output_tokens": 50}]
    result = render_llm_calls(json.dumps(calls), _PLAIN)
    assert "planner" in result
    assert "->" in result
    assert "\033[" not in result


def test_render_llm_calls_invalid_json():
    """Returns empty string for invalid JSON."""
    assert render_llm_calls("not json", _COLOR) == ""


def test_render_llm_calls_model_without_slash():
    """Model without slash is shown as-is."""
    import json
    calls = [{"role": "worker", "model": "gpt-4", "input_tokens": 100, "output_tokens": 50}]
    result = render_llm_calls(json.dumps(calls), _PLAIN)
    assert "gpt-4" in result


# ── _render_markdown ────────────────────────────────────────


def test_render_markdown_plain_text():
    """Plain text passes through without spurious formatting."""
    result = _render_markdown("Hello world", _PLAIN)
    assert "Hello world" in result


def test_render_markdown_bold_italic():
    """ANSI codes are present when color=True and tty=True."""
    result = _render_markdown("This is **bold** text", _COLOR)
    assert "\033[" in result
    assert "bold" in result


def test_render_markdown_heading():
    """Headings produce box-drawing or rule characters."""
    result = _render_markdown("# My Heading", _COLOR)
    assert "My Heading" in result


def test_render_markdown_code_block():
    """Code fences are rendered (content is visible)."""
    result = _render_markdown("```python\nprint(42)\n```", _COLOR)
    assert "print" in result
    assert "42" in result


def test_render_markdown_list():
    """Bullet list items are visible."""
    result = _render_markdown("- alpha\n- beta\n- gamma", _COLOR)
    assert "alpha" in result
    assert "beta" in result
    assert "gamma" in result


def test_render_markdown_no_color():
    """No ANSI escape sequences with color=False."""
    result = _render_markdown("**bold** and *italic*", _PLAIN)
    assert "\033[" not in result
    assert "bold" in result
    assert "italic" in result


def test_render_markdown_ascii_heading():
    """ASCII box-drawing with unicode=False (no Unicode box chars)."""
    caps = _caps(color=False, unicode=False, tty=False)
    result = _render_markdown("# Title", caps)
    assert "Title" in result
    # Should NOT contain Unicode box-drawing characters
    assert "━" not in result
    assert "┃" not in result


def test_render_markdown_respects_width():
    """Output respects the width from caps."""
    narrow = _caps(width=40, color=False, tty=False)
    result = _render_markdown("A short line", narrow)
    for line in result.splitlines():
        assert len(line) <= 40


def test_render_markdown_empty():
    """Empty string input produces empty output."""
    assert _render_markdown("", _COLOR) == ""
    assert _render_markdown("", _PLAIN) == ""


def test_render_markdown_table():
    """Tables render with visible content."""
    md = "| Name | Age |\n|------|-----|\n| Alice | 30 |"
    result = _render_markdown(md, _COLOR)
    assert "Alice" in result
    assert "30" in result


# ── render_msg_output + markdown integration ────────────────


def test_render_msg_output_markdown_formatting():
    """Full output with heading, bold, and list is rendered."""
    text = "# Hello\n\nThis is **bold** and a list:\n- item 1\n- item 2"
    result = render_msg_output(text, _COLOR)
    assert "Bot:" in result
    assert "Hello" in result
    assert "item 1" in result
    assert "item 2" in result


def test_render_msg_output_markdown_no_color():
    """Graceful degradation: content visible without ANSI escapes."""
    text = "# Hello\n\nSome **bold** text"
    result = render_msg_output(text, _PLAIN)
    assert "Bot:" in result
    assert "Hello" in result
    assert "bold" in result
    assert "\033[" not in result


def test_render_msg_output_label_on_own_line():
    """Label and body are on separate lines."""
    result = render_msg_output("Hello world", _PLAIN)
    lines = result.splitlines()
    # First line should be the label
    assert "Bot:" in lines[0]
    # Body should be on subsequent line(s)
    body_lines = lines[1:]
    body_text = "\n".join(body_lines)
    assert "Hello world" in body_text


def test_render_msg_output_with_stored_thinking():
    """Pre-extracted thinking parameter is used instead of tag extraction."""
    result = render_msg_output("clean answer", _COLOR, "Kiso", thinking="stored thought")
    assert "Thinking..." in result
    assert "stored thought" in result
    assert "clean answer" in result


def test_render_msg_output_stored_thinking_uses_output_as_is():
    """When thinking is pre-provided, output is already clean (stripped upstream)."""
    result = render_msg_output("clean answer", _COLOR, "Kiso", thinking="real thought")
    assert "Thinking..." in result
    assert "real thought" in result
    assert "clean answer" in result


def test_render_msg_output_stored_thinking_empty_skips():
    """Empty stored thinking falls back to tag extraction."""
    result = render_msg_output("<think>from tags</think>body", _COLOR, thinking="")
    # Empty string means no thinking — should still extract from tags
    assert "Thinking..." not in result  # empty thinking = no display


# ── render_llm_calls_verbose ────────────────────────────────


def test_render_llm_calls_verbose_with_messages():
    """Produces non-empty output with panel borders when messages are present."""
    import json
    calls = [
        {
            "role": "planner",
            "model": "deepseek/deepseek-v3",
            "input_tokens": 400,
            "output_tokens": 80,
            "messages": [{"role": "user", "content": "hello"}],
            "response": "world",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    assert result != ""
    assert "planner" in result
    assert "hello" in result
    assert "world" in result


def test_render_llm_calls_verbose_json_response_pretty_printed():
    """JSON responses are pretty-printed."""
    import json
    calls = [
        {
            "role": "planner",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [{"role": "user", "content": "plan"}],
            "response": '{"goal":"test","tasks":[]}',
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    assert '"goal": "test"' in result  # pretty-printed with spaces


def test_render_llm_calls_verbose_text_response():
    """Plain text responses shown as-is in panel."""
    import json
    calls = [
        {
            "role": "worker",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [{"role": "system", "content": "You are helpful"}],
            "response": "This is plain text, not JSON",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN)
    assert "This is plain text, not JSON" in result


def test_render_llm_calls_verbose_none():
    """Returns empty string for None input."""
    assert render_llm_calls_verbose(None, _COLOR) == ""


def test_render_llm_calls_verbose_empty():
    """Returns empty string for empty list."""
    assert render_llm_calls_verbose("[]", _COLOR) == ""
    assert render_llm_calls_verbose("", _COLOR) == ""


def test_render_llm_calls_verbose_no_messages():
    """Calls without messages (compact data only) return empty string."""
    import json
    calls = [
        {
            "role": "planner",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    assert result == ""


def test_render_llm_calls_verbose_non_tty():
    """Non-TTY output has no ANSI codes but still has panel structure."""
    import json
    calls = [
        {
            "role": "reviewer",
            "model": "gpt-4",
            "input_tokens": 200,
            "output_tokens": 60,
            "messages": [{"role": "user", "content": "review this"}],
            "response": "looks good",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN)
    assert result != ""
    assert "\033[" not in result
    assert "review this" in result
    assert "looks good" in result


def test_render_llm_calls_verbose_invalid_json():
    """Returns empty string for invalid JSON."""
    assert render_llm_calls_verbose("not json", _COLOR) == ""


def test_render_llm_calls_verbose_split_panels():
    """Input and output are in separate panels (no combined separator)."""
    import json
    calls = [
        {
            "role": "planner",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "plan something"},
            ],
            "response": "here is the plan",
        },
    ]
    # Unicode mode — two panels with IN / OUT direction labels
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    assert ") IN" in result   # direction label in input panel title
    assert ") OUT" in result  # direction label in output panel title
    assert "You are helpful" in result
    assert "here is the plan" in result
    # Old combined separator must be gone
    assert "\u2500\u2500\u2500 response " not in result
    # ASCII mode
    result_ascii = render_llm_calls_verbose(json.dumps(calls), _PLAIN)
    assert ") IN" in result_ascii
    assert ") OUT" in result_ascii
    assert "--- response " not in result_ascii


def test_render_llm_calls_verbose_input_only_in_input_panel():
    """Input panel contains messages, output panel does not."""
    import json
    calls = [
        {
            "role": "reviewer",
            "model": "test/m",
            "input_tokens": 200,
            "output_tokens": 30,
            "messages": [{"role": "user", "content": "UNIQUE_INPUT_MARKER"}],
            "response": "UNIQUE_OUTPUT_MARKER",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN)
    # Both markers present overall
    assert "UNIQUE_INPUT_MARKER" in result
    assert "UNIQUE_OUTPUT_MARKER" in result
    # Input marker appears before OUT direction label, output marker after
    in_pos = result.index("UNIQUE_INPUT_MARKER")
    out_label_pos = result.index(") OUT")
    out_pos = result.index("UNIQUE_OUTPUT_MARKER")
    assert in_pos < out_label_pos < out_pos


def test_render_llm_calls_verbose_summary_between_panels():
    """A compact summary line with tokens appears between the two panels."""
    import json
    calls = [
        {
            "role": "translator",
            "model": "deepseek/deepseek-v3",
            "input_tokens": 300,
            "output_tokens": 45,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "hello",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN)
    # Summary line should contain token counts and model
    assert "300" in result
    assert "45" in result
    assert "deepseek-v3" in result


# ── render_llm_call_input_panel / render_llm_call_output_panel ──


def test_render_llm_call_input_panel_shows_messages():
    """Input panel renders messages and IN direction label."""
    call = {
        "role": "translator",
        "model": "test/model-v1",
        "input_tokens": 400,
        "messages": [
            {"role": "system", "content": "You translate."},
            {"role": "user", "content": "Translate this."},
        ],
    }
    result = render_llm_call_input_panel(call, _PLAIN)
    assert "translator" in result
    assert ") IN" in result
    assert "You translate." in result
    assert "Translate this." in result
    # No output content
    assert ") OUT" not in result


def test_render_llm_call_input_panel_no_messages():
    """Returns empty string when call has no messages."""
    call = {"role": "planner", "model": "m", "input_tokens": 0}
    assert render_llm_call_input_panel(call, _PLAIN) == ""


def test_render_llm_call_output_panel_shows_response():
    """Output panel renders response and OUT direction label."""
    call = {
        "role": "reviewer",
        "model": "test/model-v1",
        "input_tokens": 200,
        "output_tokens": 30,
        "messages": [{"role": "user", "content": "check"}],
        "response": '{"status": "ok"}',
    }
    result = render_llm_call_output_panel(call, _PLAIN)
    assert "reviewer" in result
    assert ") OUT" in result
    assert '"status": "ok"' in result  # pretty-printed JSON
    # No input content
    assert ") IN" not in result
    assert "check" not in result


def test_render_llm_call_output_panel_with_thinking():
    """Output panel includes thinking/reasoning block."""
    call = {
        "role": "worker",
        "model": "deepseek/r1",
        "input_tokens": 300,
        "output_tokens": 100,
        "messages": [{"role": "user", "content": "solve"}],
        "response": "42",
        "thinking": "step by step reasoning",
    }
    result = render_llm_call_output_panel(call, _PLAIN)
    assert "reasoning" in result
    assert "step by step reasoning" in result
    assert "42" in result


def test_render_llm_call_output_panel_no_messages():
    """Returns empty string when call has no messages."""
    call = {"role": "planner", "model": "m", "output_tokens": 0}
    assert render_llm_call_output_panel(call, _PLAIN) == ""


def test_render_llm_calls_verbose_escapes_markup():
    """Content with Rich markup-like brackets is rendered literally."""
    import json
    calls = [
        {
            "role": "planner",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [{"role": "user", "content": "use [bold]text[/bold] here"}],
            "response": "ok [red]done[/red]",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    # Brackets must appear literally, not interpreted as Rich styling
    assert "[bold]" in result
    assert "[red]" in result


def test_render_llm_calls_verbose_thinking_panel():
    """Thinking block displayed in output panel before response."""
    import json
    calls = [
        {
            "role": "worker",
            "model": "deepseek/deepseek-r1",
            "input_tokens": 500,
            "output_tokens": 200,
            "messages": [{"role": "user", "content": "solve this"}],
            "response": "the answer is 42",
            "thinking": "let me think step by step...",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    assert "reasoning" in result
    assert "let me think step by step..." in result
    assert "the answer is 42" in result
    # Thinking appears before response in the output panel
    think_pos = result.index("reasoning")
    answer_pos = result.index("the answer is 42")
    assert think_pos < answer_pos


def test_render_llm_calls_verbose_thinking_absent():
    """No thinking block when thinking field is empty or missing."""
    import json
    calls = [
        {
            "role": "worker",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "hello",
            "thinking": "",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    assert "reasoning" not in result
    # Missing field entirely
    calls2 = [
        {
            "role": "worker",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "hello",
        },
    ]
    result2 = render_llm_calls_verbose(json.dumps(calls2), _COLOR)
    assert "reasoning" not in result2


def test_render_llm_calls_verbose_thinking_ascii():
    """Thinking block uses ASCII icon in plain mode."""
    import json
    calls = [
        {
            "role": "worker",
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "hello",
            "thinking": "deep reasoning",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN)
    assert "reasoning" in result
    assert "deep reasoning" in result
    assert "?" in result  # ASCII icon


# ── M101: verbose timestamp + skip ───────────────────────────


def test_render_llm_calls_verbose_timestamp():
    """Call with 'ts' epoch → HH:MM:SS appears in panel title."""
    import json
    calls = [
        {
            "role": "translator",
            "model": "deepseek-v3",
            "input_tokens": 300,
            "output_tokens": 45,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "hello",
            "ts": 1700000000.0,
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    assert "22:13:20" in result


def test_render_llm_calls_verbose_no_timestamp():
    """Call without 'ts' → no HH:MM:SS but role+model+tokens still present."""
    import json
    calls = [
        {
            "role": "translator",
            "model": "deepseek-v3",
            "input_tokens": 300,
            "output_tokens": 45,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "hello",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _COLOR)
    # No timestamp pattern HH:MM:SS
    import re
    assert not re.search(r"\d{2}:\d{2}:\d{2}", result)
    # But role, model, tokens are present
    assert "translator" in result
    assert "deepseek-v3" in result
    assert "300" in result


def test_render_llm_calls_verbose_skip_first():
    """skip=2 → only the third call is rendered."""
    import json
    calls = [
        {
            "role": "translator",
            "model": "m1",
            "input_tokens": 10,
            "output_tokens": 5,
            "messages": [{"role": "user", "content": "first"}],
            "response": "r1",
        },
        {
            "role": "reviewer",
            "model": "m2",
            "input_tokens": 20,
            "output_tokens": 10,
            "messages": [{"role": "user", "content": "second"}],
            "response": "r2",
        },
        {
            "role": "curator",
            "model": "m3",
            "input_tokens": 30,
            "output_tokens": 15,
            "messages": [{"role": "user", "content": "third"}],
            "response": "r3",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN, skip=2)
    assert "curator" in result
    assert "third" in result
    assert "translator" not in result
    assert "reviewer" not in result


def test_render_llm_calls_verbose_skip_all():
    """skip >= call count → empty output."""
    import json
    calls = [
        {
            "role": "translator",
            "model": "m1",
            "input_tokens": 10,
            "output_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "ok",
        },
        {
            "role": "reviewer",
            "model": "m2",
            "input_tokens": 20,
            "output_tokens": 10,
            "messages": [{"role": "user", "content": "check"}],
            "response": "ok",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN, skip=2)
    assert result == ""


def test_render_llm_calls_verbose_skip_zero():
    """skip=0 → all calls rendered."""
    import json
    calls = [
        {
            "role": "translator",
            "model": "m1",
            "input_tokens": 10,
            "output_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
            "response": "ok",
        },
        {
            "role": "reviewer",
            "model": "m2",
            "input_tokens": 20,
            "output_tokens": 10,
            "messages": [{"role": "user", "content": "check"}],
            "response": "fine",
        },
    ]
    result = render_llm_calls_verbose(json.dumps(calls), _PLAIN, skip=0)
    assert "translator" in result
    assert "reviewer" in result


# ── M31: search icon + substatus ──────────────────────────────


def test_search_icon_unicode():
    """Search task uses magnifying glass icon in unicode mode."""
    caps = _caps(unicode=True)
    assert _icon("search", caps) == "\U0001f50d"


def test_search_icon_ascii():
    """Search task uses S icon in ASCII mode."""
    caps = _caps(unicode=False)
    assert _icon("search", caps) == "S"


def test_task_header_search_type():
    """Search task renders with search icon."""
    task = {"type": "search", "detail": "best SEO agencies", "status": "running"}
    result = render_task_header(task, 1, 2, _COLOR)
    assert "\U0001f50d" in result
    assert "search: best SEO agencies" in result


def test_task_header_with_substatus():
    """Spinner shows substatus text when present."""
    task = {"type": "exec", "detail": "ls", "status": "running", "substatus": "translating"}
    result = render_task_header(task, 1, 2, _COLOR, spinner_frame="\u280b")
    assert "translating" in result
    assert "\u280b" in result


def test_task_header_substatus_searching():
    """Search task with searching substatus shows label."""
    task = {"type": "search", "detail": "find info", "status": "running", "substatus": "searching"}
    result = render_task_header(task, 1, 2, _COLOR, spinner_frame="\u280b")
    assert "searching" in result


def test_task_header_substatus_reviewing():
    """Reviewing substatus shows 'reviewing' label."""
    task = {"type": "exec", "detail": "ls", "status": "running", "substatus": "reviewing"}
    result = render_task_header(task, 1, 2, _COLOR, spinner_frame="\u280b")
    assert "reviewing" in result


def test_task_header_substatus_executing():
    """Executing substatus shows 'running' label."""
    task = {"type": "exec", "detail": "ls", "status": "running", "substatus": "executing"}
    result = render_task_header(task, 1, 2, _COLOR, spinner_frame="\u280b")
    assert "running" in result


def test_task_header_no_substatus():
    """Empty substatus = spinner only (backwards compatible)."""
    task = {"type": "exec", "detail": "ls", "status": "running"}
    result = render_task_header(task, 1, 2, _COLOR, spinner_frame="\u280b")
    assert "\u280b" in result
    assert "translating" not in result
    assert "reviewing" not in result
    assert "searching" not in result
    assert "composing" not in result


# ── M33: render_review retry indicator ───────────────────────


def test_render_review_ok_after_retry():
    """OK verdict with retry_count > 0 shows '(after N retries)'."""
    task = {"review_verdict": "ok", "retry_count": 2}
    result = render_review(task, _COLOR)
    assert "review: ok" in result
    assert "(after 2 retries)" in result


def test_render_review_ok_after_single_retry():
    """OK verdict with retry_count = 1 shows '(after 1 retry)' (singular)."""
    task = {"review_verdict": "ok", "retry_count": 1}
    result = render_review(task, _COLOR)
    assert "(after 1 retry)" in result


def test_render_review_replan_with_retry_count():
    """Replan verdict with retry_count > 0 shows escalation message."""
    task = {"review_verdict": "replan", "review_reason": "still broken", "retry_count": 1}
    result = render_review(task, _COLOR)
    assert "replan" in result
    assert "(retried 1x before escalating)" in result


def test_render_review_no_retry_no_indicator():
    """No retry_count or retry_count=0 → no retry indicator."""
    task = {"review_verdict": "ok"}
    result = render_review(task, _COLOR)
    assert "retry" not in result.lower()

    task2 = {"review_verdict": "ok", "retry_count": 0}
    result2 = render_review(task2, _COLOR)
    assert "retry" not in result2.lower()

    task3 = {"review_verdict": "replan", "review_reason": "fail", "retry_count": 0}
    result3 = render_review(task3, _COLOR)
    assert "retried" not in result3.lower()


# ── M98: _parse_llm_calls + get_last_thinking ────────────────


class TestParseLlmCalls:
    def test_none_returns_empty(self):
        assert _parse_llm_calls(None) == []

    def test_empty_string_returns_empty(self):
        assert _parse_llm_calls("") == []

    def test_invalid_json_returns_empty(self):
        assert _parse_llm_calls("not json") == []

    def test_non_list_json_returns_empty(self):
        assert _parse_llm_calls('{"key": "val"}') == []

    def test_valid_list(self):
        import json
        calls = [{"role": "planner", "model": "gpt-4"}]
        assert _parse_llm_calls(json.dumps(calls)) == calls

    def test_empty_list(self):
        assert _parse_llm_calls("[]") == []


class TestGetLastThinking:
    def test_none_returns_none(self):
        assert get_last_thinking(None) is None

    def test_empty_calls_returns_none(self):
        assert get_last_thinking("[]") is None

    def test_missing_thinking_field(self):
        import json
        calls = [{"role": "worker", "response": "hi"}]
        assert get_last_thinking(json.dumps(calls)) is None

    def test_empty_thinking_field(self):
        import json
        calls = [{"role": "worker", "thinking": ""}]
        assert get_last_thinking(json.dumps(calls)) is None

    def test_returns_last_call_thinking(self):
        import json
        calls = [
            {"role": "translator", "thinking": "first thought"},
            {"role": "reviewer", "thinking": "second thought"},
        ]
        assert get_last_thinking(json.dumps(calls)) == "second thought"

    def test_invalid_json_returns_none(self):
        assert get_last_thinking("broken") is None


# ── M98: extract_thinking edge cases ─────────────────────────


def test_extract_thinking_whitespace_only():
    """Whitespace-only blocks are stripped to empty."""
    thinking, clean = extract_thinking("<think>   \n  </think>hello")
    assert thinking == ""
    assert clean == "hello"


def test_extract_thinking_nested_tags():
    """Nested tags: inner tags are captured as literal text, not parsed."""
    text = "<think>outer <think>inner</think> rest</think>body"
    thinking, clean = extract_thinking(text)
    # Regex is non-greedy, so first match is "outer <think>inner"
    assert "outer" in thinking
    assert "body" in clean


def test_extract_thinking_unclosed_tag():
    """Unclosed <think> tag is not matched — text returned as-is."""
    text = "<think>no closing tag here"
    thinking, clean = extract_thinking(text)
    assert thinking == ""
    assert clean == text


def test_extract_thinking_unicode_content():
    """Unicode content inside thinking tags is preserved."""
    text = "<think>考えています 🤔</think>答え"
    thinking, clean = extract_thinking(text)
    assert "考えています" in thinking
    assert clean == "答え"
