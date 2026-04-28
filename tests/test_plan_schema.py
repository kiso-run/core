"""M1579a — `awaits_input` plan field.

The broker model needs a plan-level signal "the planner is pausing
to ask the user something". Today the validator rejects msg-only
plans unless one of `needs_install` / `knowledge` / `kb_answer` /
`allow_msg_only` is set; that means the planner has to fake one of
those flags to ask a clarification question, which is paradigm-
mismatched.

`awaits_input` is the generalist signal — works for ANY capability-
missing or clarification scenario. Schema field, validation rule,
DB column, store helper. M1579a is the foundation; M1579c sets the
field from the planner; M1579d reads it from chat_kb fallback.
"""

from __future__ import annotations

from kiso.brain.common import PLAN_SCHEMA
from kiso.brain.planner import _validate_plan_ordering, validate_plan


def _make_plan(**overrides) -> dict:
    base = {
        "goal": "ask user something",
        "secrets": None,
        "tasks": [{
            "type": "msg", "detail": "Answer in English. which file?",
            "args": None, "expect": None,
        }],
        "extend_replan": None,
        "needs_install": None,
        "knowledge": None,
        "kb_answer": None,
        "awaits_input": None,
    }
    base.update(overrides)
    return base


class TestAwaitsInputSchemaField:
    """The PLAN_SCHEMA exposes `awaits_input` as a top-level required
    field of type bool|null."""

    def test_field_in_schema_properties(self):
        schema = PLAN_SCHEMA["json_schema"]["schema"]
        props = schema["properties"]
        assert "awaits_input" in props
        # bool | null shape (parallels kb_answer)
        any_of = props["awaits_input"]["anyOf"]
        types = {entry.get("type") for entry in any_of}
        assert types == {"boolean", "null"}

    def test_field_is_required(self):
        schema = PLAN_SCHEMA["json_schema"]["schema"]
        assert "awaits_input" in schema["required"]


class TestAwaitsInputValidation:
    """validate_plan accepts msg-only plans when `awaits_input=true`,
    and rejects the field if mixed with action tasks (coherence)."""

    def test_msg_only_with_awaits_input_true_validates(self):
        plan = _make_plan(awaits_input=True)
        errors = validate_plan(plan)
        # The msg-only-rejection error must NOT fire.
        assert not any("Plan has only msg tasks" in e for e in errors), (
            f"unexpected msg-only rejection: {errors}"
        )

    def test_msg_only_with_awaits_input_false_still_rejected(self):
        """Without any escape flag, msg-only stays rejected."""
        plan = _make_plan(awaits_input=False)
        errors = validate_plan(plan)
        assert any("Plan has only msg tasks" in e for e in errors), (
            f"expected msg-only rejection, got: {errors}"
        )

    def test_no_awaits_input_field_defaults_false(self):
        """Plan dict with the key entirely absent behaves like
        awaits_input=false (backward-compat)."""
        plan = _make_plan()
        plan.pop("awaits_input", None)
        errors = validate_plan(plan)
        assert any("Plan has only msg tasks" in e for e in errors)

    def test_awaits_input_true_with_exec_fails_coherence(self):
        """awaits_input=true must be msg-only — mixing it with an
        action task is incoherent (the plan has work to do, the
        planner is not actually pausing)."""
        plan = _make_plan(
            awaits_input=True,
            tasks=[
                {"type": "msg", "detail": "Answer in English. which file?",
                 "args": None, "expect": None},
                {"type": "exec", "detail": "ls /tmp",
                 "args": None, "expect": "files listed"},
            ],
        )
        errors = validate_plan(plan)
        assert any("awaits_input" in e for e in errors), (
            f"expected coherence error, got: {errors}"
        )


class TestAwaitsInputOrderingHelper:
    """`_validate_plan_ordering` accepts an `has_awaits_input` kwarg
    that mirrors the existing `has_kb_answer` escape hatch."""

    def test_msg_only_allowed_awaits_input(self):
        tasks = [{"type": "msg", "detail": "Answer in English. which file?"}]
        errors = _validate_plan_ordering(
            tasks, is_replan=False, install_approved=False,
            has_awaits_input=True,
        )
        assert not any("Plan has only msg tasks" in e for e in errors)
