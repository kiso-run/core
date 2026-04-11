"""Guard tests for intentional public import surfaces.

These tests protect the boundaries we want to keep stable during the
complexity-reduction refactors. They are intentionally narrow: they do not try
to freeze every helper exported by a large module.
"""

from __future__ import annotations

from unittest.mock import sentinel

from fastapi import FastAPI

import kiso.brain as brain
import kiso.main as main
import kiso.worker as worker


_BRAIN_PUBLIC_NAMES = {
    "BrieferError",
    "ClassifierError",
    "CuratorError",
    "ExecTranslatorError",
    "MessengerError",
    "ParaphraserError",
    "PlanError",
    "ReviewError",
    "SearcherError",
    "SummarizerError",
    "build_classifier_messages",
    "build_exec_translator_messages",
    "build_messenger_messages",
    "build_planner_messages",
    "build_recent_context",
    "build_reviewer_messages",
    "classify_failure_class",
    "invalidate_prompt_cache",
    "is_stop_message",
    "run_briefer",
    "run_classifier",
    "run_curator",
    "run_inflight_classifier",
    "run_messenger",
    "run_paraphraser",
    "run_planner",
    "run_reviewer",
    "run_searcher",
    "run_summarizer",
    "run_worker",
    "validate_briefing",
    "validate_curator",
    "validate_plan",
    "validate_review",
}


def test_worker_public_surface_is_minimal():
    """`kiso.worker` exposes only the real runtime entrypoint."""
    assert worker.__all__ == ["run_worker"]
    assert callable(worker.run_worker)
    assert not hasattr(worker, "_handle_wrapper_task")
    assert not hasattr(worker, "_build_execution_state")


def test_main_runtime_entrypoints_exist():
    """`kiso.main` keeps the ASGI app and startup-state seam importable."""
    assert isinstance(main.app, FastAPI)
    assert callable(main._init_app_state)


def test_brain_high_level_surface_remains_importable():
    """`kiso.brain` keeps the high-level orchestration surface stable."""
    missing = sorted(name for name in _BRAIN_PUBLIC_NAMES if not hasattr(brain, name))
    assert missing == []


def test_brain_validate_plan_patch_propagates_to_planner_module():
    """`patch("kiso.brain.validate_plan", ...)` must still affect run_planner internals."""
    original = brain.validate_plan
    try:
        brain.validate_plan = sentinel.validate_plan
        import kiso.brain.planner as planner

        assert planner.validate_plan is sentinel.validate_plan
    finally:
        brain.validate_plan = original
