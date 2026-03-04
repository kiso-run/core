"""Tests for cli.render utility functions."""

from __future__ import annotations

import re

from cli.render import (
    TermCaps,
    _fmt_duration,
    render_cancel_done,
    render_inflight_call,
    render_llm_calls_verbose,
    render_phase_done,
    render_planner_spinner,
    render_step_usage,
    render_task_header,
)

# Strip ANSI escape codes for plain-text assertions
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


_UNICODE_CAPS = TermCaps(color=True, unicode=True, width=80, height=24, tty=True)
_PLAIN_CAPS = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)


# ── _fmt_duration (M110a) ────────────────────────────────────


class TestFmtDuration:
    def test_zero_seconds(self):
        assert _fmt_duration(0) == "0s"

    def test_under_minute(self):
        assert _fmt_duration(45) == "45s"

    def test_exact_minute(self):
        assert _fmt_duration(60) == "1m"

    def test_minutes_and_seconds(self):
        assert _fmt_duration(90) == "1m 30s"

    def test_multiple_minutes(self):
        assert _fmt_duration(150) == "2m 30s"

    def test_exact_multiple_minutes(self):
        assert _fmt_duration(120) == "2m"


# ── render_step_usage ────────────────────────────────────────


class TestRenderStepUsage:
    def test_both_zero_returns_empty(self):
        assert render_step_usage(0, 0, _UNICODE_CAPS) == ""

    def test_unicode_format(self):
        result = _plain(render_step_usage(430, 85, _UNICODE_CAPS))
        assert result == "⟨430→85⟩"

    def test_plain_format(self):
        result = _plain(render_step_usage(430, 85, _PLAIN_CAPS))
        assert result == "<430->85>"

    def test_large_numbers_formatted_with_commas(self):
        result = _plain(render_step_usage(1_000, 2_000, _PLAIN_CAPS))
        assert "1,000" in result
        assert "2,000" in result

    def test_only_input_nonzero(self):
        # Either token being nonzero should produce output
        assert render_step_usage(100, 0, _PLAIN_CAPS) != ""

    def test_only_output_nonzero(self):
        assert render_step_usage(0, 50, _PLAIN_CAPS) != ""


# ── render_cancel_done ───────────────────────────────────────


class TestRenderCancelDone:
    def test_basic_header(self):
        result = _plain(render_cancel_done(2, 5, [], [], _PLAIN_CAPS))
        assert "2 of 5 tasks" in result

    def test_done_tasks_listed(self):
        result = _plain(render_cancel_done(2, 5, ["task1", "task2"], [], _PLAIN_CAPS))
        assert "Done: task1, task2" in result

    def test_skipped_tasks_listed(self):
        result = _plain(render_cancel_done(0, 3, [], ["a", "b"], _PLAIN_CAPS))
        assert "Skipped: a, b" in result

    def test_no_done_section_when_empty(self):
        result = _plain(render_cancel_done(0, 2, [], ["x"], _PLAIN_CAPS))
        assert "Done:" not in result

    def test_no_skipped_section_when_empty(self):
        result = _plain(render_cancel_done(1, 2, ["y"], [], _PLAIN_CAPS))
        assert "Skipped:" not in result

    def test_both_done_and_skipped(self):
        result = _plain(render_cancel_done(1, 3, ["done1"], ["skip1", "skip2"], _PLAIN_CAPS))
        assert "Done: done1" in result
        assert "Skipped: skip1, skip2" in result


# ── render_phase_done (M110a) ────────────────────────────────


class TestRenderPhaseDone:
    def test_classifying_phase(self):
        result = _plain(render_phase_done("classifying", 2.5, _PLAIN_CAPS))
        assert "Classified" in result
        assert "in 2s" in result

    def test_planning_phase(self):
        result = _plain(render_phase_done("planning", 150.0, _PLAIN_CAPS))
        assert "Planned" in result
        assert "in 2m 30s" in result

    def test_executing_phase(self):
        result = _plain(render_phase_done("executing", 12.0, _PLAIN_CAPS))
        assert "Executed" in result
        assert "in 12s" in result

    def test_idle_returns_empty(self):
        assert render_phase_done("idle", 5.0, _PLAIN_CAPS) == ""

    def test_exact_minute(self):
        result = _plain(render_phase_done("planning", 60.0, _PLAIN_CAPS))
        assert "in 1m" in result
        assert "0s" not in result

    def test_contains_check_icon(self):
        result = _plain(render_phase_done("classifying", 3.0, _UNICODE_CAPS))
        assert "✓" in result


# ── render_planner_spinner (M109c) ───────────────────────────


class TestRenderPlannerSpinner:
    def test_default_phase_is_planning(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|"))
        assert "Planning" in result

    def test_classifying_phase(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|", phase="classifying"))
        assert "Classifying" in result

    def test_executing_phase(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|", phase="executing"))
        assert "Executing" in result

    def test_idle_phase_shows_waiting(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|", phase="idle"))
        assert "Waiting" in result

    def test_unknown_phase_capitalized(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|", phase="reviewing"))
        assert "Reviewing" in result

    def test_elapsed_shown(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|", elapsed=15, phase="planning"))
        assert "for 15s" in result

    def test_elapsed_under_2s_hidden(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|", elapsed=1, phase="planning"))
        assert "for " not in result

    def test_phase_with_elapsed_combined(self):
        result = _plain(render_planner_spinner(_PLAIN_CAPS, "|", elapsed=30, phase="classifying"))
        assert "Classifying" in result
        assert "for 30s" in result


# ── render_inflight_call (M109c) ─────────────────────────────


class TestRenderInflightCall:
    _CALL = {
        "role": "planner",
        "model": "deepseek/deepseek-v3",
        "messages": [
            {"role": "system", "content": "You are a planner."},
            {"role": "user", "content": "Deploy the app."},
        ],
        "ts": 1709553600.0,
    }

    def test_contains_role_and_model(self):
        result = _plain(render_inflight_call(self._CALL, _PLAIN_CAPS))
        assert "planner" in result
        assert "deepseek-v3" in result

    def test_contains_waiting_label(self):
        result = _plain(render_inflight_call(self._CALL, _PLAIN_CAPS))
        assert "waiting" in result.lower()

    def test_contains_messages(self):
        result = _plain(render_inflight_call(self._CALL, _PLAIN_CAPS))
        assert "You are a planner." in result
        assert "Deploy the app." in result

    def test_unicode_uses_hourglass(self):
        result = render_inflight_call(self._CALL, _UNICODE_CAPS)
        assert "\u23f3" in result  # ⏳

    def test_no_messages_still_renders(self):
        call = {"role": "planner", "model": "gpt-4", "messages": [], "ts": None}
        result = _plain(render_inflight_call(call, _PLAIN_CAPS))
        assert "planner" in result
        assert "waiting" in result.lower()


# ── render_task_header duration (M111b) ──────────────────────


class TestRenderTaskHeaderDuration:
    def test_done_with_duration(self):
        task = {"status": "done", "type": "exec", "detail": "ls", "duration_ms": 5000}
        result = _plain(render_task_header(task, 1, 3, _PLAIN_CAPS))
        assert "(5s)" in result

    def test_done_under_1s_hidden(self):
        task = {"status": "done", "type": "exec", "detail": "ls", "duration_ms": 500}
        result = _plain(render_task_header(task, 1, 3, _PLAIN_CAPS))
        assert "(" not in result

    def test_done_without_duration(self):
        task = {"status": "done", "type": "exec", "detail": "ls", "duration_ms": None}
        result = _plain(render_task_header(task, 1, 3, _PLAIN_CAPS))
        assert "(" not in result

    def test_failed_with_duration(self):
        task = {"status": "failed", "type": "exec", "detail": "bad", "duration_ms": 12000}
        result = _plain(render_task_header(task, 1, 3, _PLAIN_CAPS))
        assert "(12s)" in result

    def test_running_ignores_duration_ms(self):
        task = {"status": "running", "type": "exec", "detail": "ls", "duration_ms": 5000}
        result = _plain(render_task_header(task, 1, 3, _PLAIN_CAPS))
        assert "(5s)" not in result

    def test_done_with_minutes_format(self):
        task = {"status": "done", "type": "exec", "detail": "build", "duration_ms": 90000}
        result = _plain(render_task_header(task, 1, 3, _PLAIN_CAPS))
        assert "(1m 30s)" in result

    def test_done_without_duration_key(self):
        """Task dict from old data without duration_ms key at all."""
        task = {"status": "done", "type": "exec", "detail": "ls"}
        result = _plain(render_task_header(task, 1, 3, _PLAIN_CAPS))
        assert "(" not in result


# ── render_llm_calls_verbose dedup (M111c) ───────────────────


class TestVerboseDedup:
    """M111c: shown_inflight_ts suppresses input messages for already-seen calls."""

    _CALLS_JSON = __import__("json").dumps([{
        "role": "translator",
        "model": "test/model",
        "input_tokens": 100,
        "output_tokens": 50,
        "messages": [{"role": "system", "content": "Translate this."}],
        "response": "ls -la",
        "ts": 1700000000.0,
    }])

    def test_without_shown_set_input_visible(self):
        result = _plain(render_llm_calls_verbose(self._CALLS_JSON, _PLAIN_CAPS))
        assert "Translate this." in result
        assert "ls -la" in result

    def test_matching_ts_input_hidden(self):
        result = _plain(render_llm_calls_verbose(
            self._CALLS_JSON, _PLAIN_CAPS,
            shown_inflight_ts={1700000000.0},
        ))
        assert "Translate this." not in result
        assert "ls -la" in result

    def test_non_matching_ts_input_visible(self):
        result = _plain(render_llm_calls_verbose(
            self._CALLS_JSON, _PLAIN_CAPS,
            shown_inflight_ts={9999999999.0},
        ))
        assert "Translate this." in result
        assert "ls -la" in result

    def test_empty_set_input_visible(self):
        """Empty set (distinct from None) still shows input."""
        result = _plain(render_llm_calls_verbose(
            self._CALLS_JSON, _PLAIN_CAPS,
            shown_inflight_ts=set(),
        ))
        assert "Translate this." in result
        assert "ls -la" in result
