"""M1591 — V4-Flash output schema robustness.

V4-Flash's json_object mode does not strictly enforce schema types.
Strings can leak into integer fields ("1" instead of 1). The strict
JSON schema may accept the dict at one validation layer (loose JSON
typing for some toolchains) but downstream consumers — SQL bindings,
list indexing, equality checks — break or behave incorrectly.

These locks pin defensive coercion in each V4-Flash consumer's
validator. The schema definitions remain strict (`{"type": "integer"}`
etc.) so drift is visible in code review; coercion is a separate,
runtime safety net inside `validate_*`.

Triggered by `TestCuratorLive.test_curator_v4_flash_verdict_mix`
returning `learning_id` as the string `"1"`. M1591 generalizes the
fix across all V4-Flash consumers (curator, planner, briefer).
"""

from __future__ import annotations

from kiso.brain.common import validate_briefing
from kiso.brain.curator import validate_curator
from kiso.brain.planner import validate_plan


def _make_eval(**overrides) -> dict:
    base = {
        "learning_id": 1,
        "verdict": "discard",
        "fact": None,
        "category": None,
        "question": None,
        "reason": "ok",
        "tags": None,
        "entity_name": None,
        "entity_kind": None,
    }
    base.update(overrides)
    return base


def _make_briefing(**overrides) -> dict:
    base = {
        "modules": [],
        "skills": [],
        "mcp_methods": [],
        "mcp_resources": [],
        "mcp_prompts": [],
        "context": "",
        "output_indices": [],
        "relevant_tags": [],
        "relevant_entities": [],
    }
    base.update(overrides)
    return base


def _make_plan(**overrides) -> dict:
    base = {
        "goal": "test goal",
        "secrets": None,
        "tasks": [{
            "type": "exec", "detail": "ls", "args": None, "expect": None,
        }],
        "extend_replan": None,
        "needs_install": None,
        "knowledge": None,
        "kb_answer": None,
    }
    base.update(overrides)
    return base


class TestCuratorIntCoercion:
    """validate_curator coerces stringified `learning_id` to int."""

    def test_string_learning_id_coerced_to_int(self):
        result = {"evaluations": [_make_eval(learning_id="1")]}
        errors = validate_curator(result, expected_count=1)
        assert errors == [], f"unexpected validation errors: {errors}"
        ev = result["evaluations"][0]
        assert ev["learning_id"] == 1
        assert isinstance(ev["learning_id"], int)

    def test_int_learning_id_unchanged(self):
        result = {"evaluations": [_make_eval(learning_id=5)]}
        errors = validate_curator(result, expected_count=1)
        assert errors == []
        assert result["evaluations"][0]["learning_id"] == 5

    def test_non_numeric_learning_id_emits_error(self):
        """Strings that cannot be coerced to int must be flagged."""
        result = {"evaluations": [_make_eval(learning_id="abc")]}
        errors = validate_curator(result, expected_count=1)
        assert any("learning_id" in e for e in errors), (
            f"expected learning_id error, got: {errors}"
        )


class TestBrieferOutputIndicesCoercion:
    """validate_briefing coerces stringified items inside output_indices."""

    def test_string_output_indices_items_coerced(self):
        briefing = _make_briefing(output_indices=["1", "2", 3])
        errors = validate_briefing(briefing, check_modules=False)
        assert errors == [], f"unexpected errors: {errors}"
        assert briefing["output_indices"] == [1, 2, 3]
        assert all(isinstance(x, int) for x in briefing["output_indices"])

    def test_int_output_indices_unchanged(self):
        briefing = _make_briefing(output_indices=[1, 2, 3])
        errors = validate_briefing(briefing, check_modules=False)
        assert errors == []
        assert briefing["output_indices"] == [1, 2, 3]

    def test_non_numeric_output_indices_item_emits_error(self):
        briefing = _make_briefing(output_indices=["abc"])
        errors = validate_briefing(briefing, check_modules=False)
        assert any("output_indices" in e for e in errors), (
            f"expected output_indices error, got: {errors}"
        )


class TestPlannerIntCoercion:
    """validate_plan coerces stringified `extend_replan` and
    `tasks[].group`."""

    def test_string_extend_replan_coerced_in_replan(self):
        plan = _make_plan(extend_replan="2")
        validate_plan(plan, is_replan=True)
        assert plan["extend_replan"] == 2
        assert isinstance(plan["extend_replan"], int)

    def test_string_task_group_coerced(self):
        plan = _make_plan(tasks=[
            {"type": "exec", "detail": "ls", "args": None,
             "expect": None, "group": "1"},
            {"type": "exec", "detail": "echo hi", "args": None,
             "expect": None, "group": "1"},
        ])
        validate_plan(plan)
        for task in plan["tasks"]:
            assert task["group"] == 1
            assert isinstance(task["group"], int)

    def test_int_extend_replan_unchanged_in_replan(self):
        plan = _make_plan(extend_replan=3)
        validate_plan(plan, is_replan=True)
        assert plan["extend_replan"] == 3
