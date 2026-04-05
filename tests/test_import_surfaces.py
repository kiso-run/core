"""Guard tests for intentional public import surfaces.

These tests protect the boundaries we want to keep stable during the
complexity-reduction refactors. They are intentionally narrow: they do not try
to freeze every helper exported by a large module.
"""

from __future__ import annotations

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
    "classify_inflight",
    "classify_message",
    "invalidate_prompt_cache",
    "is_stop_message",
    "run_briefer",
    "run_curator",
    "run_exec_translator",
    "run_messenger",
    "run_paraphraser",
    "run_planner",
    "run_reviewer",
    "run_searcher",
    "run_summarizer",
    "validate_briefing",
    "validate_curator",
    "validate_plan",
    "validate_review",
}


def test_worker_public_surface_is_minimal():
    """`kiso.worker` exposes only the real runtime entrypoint."""
    assert worker.__all__ == ["run_worker"]
    assert callable(worker.run_worker)
    assert not hasattr(worker, "_handle_tool_task")
    assert not hasattr(worker, "_build_execution_state")


def test_main_runtime_entrypoints_exist():
    """`kiso.main` keeps the ASGI app and startup-state seam importable."""
    assert isinstance(main.app, FastAPI)
    assert callable(main._init_app_state)


def test_brain_high_level_surface_remains_importable():
    """`kiso.brain` keeps the high-level orchestration surface stable."""
    missing = sorted(name for name in _BRAIN_PUBLIC_NAMES if not hasattr(brain, name))
    assert missing == []
