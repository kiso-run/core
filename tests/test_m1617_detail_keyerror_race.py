"""M1617 — KeyError 'detail' race in replan-context formatting.

The post-v0.11 full-suite run surfaced an F7 failure mode where the
worker's replan-context builder crashed with ``KeyError: 'detail'``
mid-test. Stack pointed into ``kiso.worker.replan._format_task_list``
and the sibling unnamed list-comp inside
``_format_completed_and_remaining`` — both did raw dict access on
``task['detail']``. Under a timeout / partial-construction race the
task row can lack the ``detail`` field, turning a meaningful
``asyncio.TimeoutError`` into a noisy ``KeyError``.

Invariant: both helpers must tolerate a task row whose ``detail``
field is missing or set to ``None`` and emit a placeholder rather
than raising. Real failures (timeouts, plan errors) must surface
unmasked.
"""

from __future__ import annotations

import pytest

from kiso.worker.replan import _format_task_list


class TestFormatTaskListSurvivesMissingDetail:
    """``_format_task_list`` must not raise when a task row lacks
    the ``detail`` key. The replan-context builder calls this on the
    ``remaining`` list which can be partial under timeout."""

    def test_missing_detail_does_not_raise_keyerror(self):
        tasks = [
            {"type": "exec", "detail": "run X"},
            {"type": "msg"},  # detail field absent — race state
        ]
        result = _format_task_list(tasks, "Remaining")
        assert "[exec] run X" in result
        assert "[msg]" in result, (
            "task without detail must still appear in the rendered "
            "list (possibly with empty placeholder), not crash"
        )

    def test_none_detail_does_not_raise(self):
        tasks = [{"type": "exec", "detail": None}]
        result = _format_task_list(tasks, "Remaining")
        assert "[exec]" in result


class TestFormatReplanTasksSurvivesMissingDetail:
    """``_format_replan_tasks`` (the replan-context's
    completed+remaining renderer) must tolerate a remaining row
    whose ``detail`` is missing under partial-construction races."""

    def test_remaining_with_missing_detail_does_not_raise(self):
        from kiso.worker.replan import _format_replan_tasks

        completed: list[dict] = []
        remaining = [
            {"type": "exec", "detail": "run Y"},
            {"type": "msg"},  # missing detail under race
        ]
        # Must not raise KeyError; the remaining-section render
        # should include both entries.
        parts = _format_replan_tasks(completed, remaining)
        joined = "\n".join(parts)
        assert "Remaining Tasks" in joined
        assert "[exec] run Y" in joined
        assert "[msg]" in joined


class TestBuildReplanContextSurvivesPartialRemaining:
    """When `run_message` hits its outer ``asyncio.wait_for`` timeout,
    the failure mode must be ``asyncio.TimeoutError``, never
    ``KeyError`` from a partially-constructed task row.

    This pins the user-visible contract — a meaningful timeout
    exception, not a noisy mask. The code-level guard is the
    helper-level fix above; this is a regression-shaped guard
    on its absence at the orchestration level."""

    def test_replan_context_render_under_partial_remaining(self):
        """Simulate the partial-construction race: a remaining
        task with no detail field. The replan-context renderer
        must produce a string, not raise."""
        from kiso.worker.replan import _build_replan_context

        completed: list[dict] = []
        remaining_partial = [{"type": "exec"}]  # no detail, no expect

        # Must not raise.
        ctx = _build_replan_context(
            completed=completed,
            remaining=remaining_partial,
            replan_reason="timed out",
            replan_history=[],
        )
        assert isinstance(ctx, str)
        assert "Remaining Tasks" in ctx
