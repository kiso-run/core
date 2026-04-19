"""Retirement invariants for the ``wrapper`` task type + subsystem.

Once part 2c-ii of the wrapper retirement lands, these tests pin the
absence of the wrapper discovery / validation / execution surface and
the wrapper task type. They should stay green forever.
"""

from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# Module / file surface
# ---------------------------------------------------------------------------


class TestWrapperModulesGone:

    def test_kiso_wrappers_deleted(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kiso.wrappers")

    def test_kiso_wrapper_repair_deleted(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kiso.wrapper_repair")

    def test_kiso_registry_deleted(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kiso.registry")

    def test_worker_wrapper_module_deleted(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kiso.worker.wrapper")


# ---------------------------------------------------------------------------
# Task-type surface
# ---------------------------------------------------------------------------


class TestWrapperTaskTypeGone:

    def test_task_type_wrapper_constant_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "TASK_TYPE_WRAPPER")

    def test_task_types_frozenset_has_no_wrapper(self):
        import kiso.brain.common as common
        assert "wrapper" not in common.TASK_TYPES

    def test_task_handlers_map_has_exact_four_types(self):
        from kiso.worker.loop import _TASK_HANDLERS
        assert set(_TASK_HANDLERS.keys()) == {"exec", "msg", "replan", "mcp"}


# ---------------------------------------------------------------------------
# Validator rejects wrapper plans
# ---------------------------------------------------------------------------


class TestValidatePlanRejectsWrapper:

    def _minimal_plan(self, task_type: str) -> dict:
        return {
            "goal": "x",
            "tasks": [
                {
                    "type": task_type,
                    "detail": "do thing",
                    "wrapper": None,
                    "args": None,
                    "expect": "done",
                }
            ],
        }

    def test_validate_plan_rejects_type_wrapper(self):
        from kiso.brain.planner import validate_plan
        errors = validate_plan(self._minimal_plan("wrapper"))
        assert errors, "expected validate_plan to reject type=wrapper"
        assert any("wrapper" in e.lower() or "type" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# kiso.brain no longer re-exports wrapper/registry helpers
# ---------------------------------------------------------------------------


class TestBrainReExportsGone:

    def test_discover_wrappers_not_re_exported(self):
        import kiso.brain as brain
        assert not hasattr(brain, "discover_wrappers")

    def test_get_registry_wrappers_not_re_exported(self):
        import kiso.brain as brain
        assert not hasattr(brain, "get_registry_wrappers")

    def test_build_planner_wrapper_list_not_re_exported(self):
        import kiso.brain as brain
        assert not hasattr(brain, "build_planner_wrapper_list")

    def test_wrapper_error_not_re_exported(self):
        import kiso.brain as brain
        assert not hasattr(brain, "WrapperError")
