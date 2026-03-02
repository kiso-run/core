"""Tests for cli.render utility functions."""

from __future__ import annotations

import re

from cli.render import TermCaps, render_cancel_done, render_step_usage

# Strip ANSI escape codes for plain-text assertions
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


_UNICODE_CAPS = TermCaps(color=True, unicode=True, width=80, height=24, tty=True)
_PLAIN_CAPS = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)


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
