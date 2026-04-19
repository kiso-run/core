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


# ---------------------------------------------------------------------------
# Install-mode constants: kiso_wrapper modes gone, pip/apt/npm modes retained
# ---------------------------------------------------------------------------


class TestInstallModeConstantsAudit:

    def test_install_mode_kiso_wrapper_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "_INSTALL_MODE_KISO_WRAPPER")

    def test_install_mode_unknown_kiso_wrapper_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "_INSTALL_MODE_UNKNOWN_KISO_WRAPPER")

    def test_install_mode_python_lib_retained(self):
        import kiso.brain.common as common
        assert common._INSTALL_MODE_PYTHON_LIB == "python_lib"

    def test_install_mode_system_pkg_retained(self):
        import kiso.brain.common as common
        assert common._INSTALL_MODE_SYSTEM_PKG == "system_pkg"

    def test_install_mode_node_cli_retained(self):
        import kiso.brain.common as common
        assert common._INSTALL_MODE_NODE_CLI == "node_cli"


# ---------------------------------------------------------------------------
# Wrapper-specific regexes, helpers, and markers gone
# ---------------------------------------------------------------------------


class TestWrapperRegexAndHelpersGone:

    def test_kiso_wrapper_signal_re_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "_KISO_WRAPPER_SIGNAL_RE")

    def test_is_explicit_named_wrapper_request_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "_is_explicit_named_wrapper_request")

    def test_parse_registry_hint_names_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "_parse_registry_hint_names")

    def test_wrapper_not_installed_marker_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "_WRAPPER_NOT_INSTALLED_MARKER")

    def test_wrapper_unavailable_marker_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "_WRAPPER_UNAVAILABLE_MARKER")


# ---------------------------------------------------------------------------
# FAILURE_CLASS_SEMANTIC_WRAPPER retired: wrapper-arg validation is gone,
# so the semantic_tool_validation classification has no producer and no
# consumer.
# ---------------------------------------------------------------------------


class TestFailureClassSemanticWrapperGone:

    def test_failure_class_constant_removed(self):
        import kiso.brain.common as common
        assert not hasattr(common, "FAILURE_CLASS_SEMANTIC_WRAPPER")

    def test_failure_classes_frozenset_has_no_semantic_wrapper(self):
        from kiso.brain.common import FAILURE_CLASSES
        assert "semantic_tool_validation" not in FAILURE_CLASSES

    def test_classify_failure_class_does_not_return_semantic_wrapper(self):
        from kiso.brain.common import classify_failure_class
        result = classify_failure_class(
            ["Wrapper args validation failed: files must contain file paths only"]
        )
        assert result != "semantic_tool_validation"


# ---------------------------------------------------------------------------
# Role prompts: retired CLI (`kiso wrapper …`) and install-flow refs purged
# ---------------------------------------------------------------------------


class TestRolePromptsWrapperPurged:

    def _read(self, name: str) -> str:
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        return (root / "kiso" / "roles" / name).read_text()

    def test_worker_prompt_no_retired_wrapper_cli(self):
        text = self._read("worker.md")
        assert "kiso wrapper install" not in text
        assert "wrapper venv" not in text.lower()
        assert "wrapper/connector names" not in text.lower()

    def test_classifier_prompt_no_wrapper_mentions(self):
        text = self._read("classifier.md").lower()
        assert "wrapper" not in text
