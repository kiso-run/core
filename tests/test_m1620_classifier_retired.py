"""M1620 — locks that pin the classifier-retirement architecture refactor.

After M1620 lands, the classifier as a separate LLM step + the chat / chat_kb
fast paths + the M1579d preflight broker pause are all gone. The worker
pipeline becomes deterministic: ``briefer → planner → execute`` for every
message, with the planner's Decision Tree as the single source of routing
truth.

These tests pin the post-refactor shape so a future "let's reintroduce a
classifier for performance" change at least has to update the locks first.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"


# ---------------------------------------------------------------------------
# Code-side deletions
# ---------------------------------------------------------------------------


def test_run_classifier_is_gone():
    """``kiso.brain.run_classifier`` is no longer importable.

    M1620 retires the classifier as a separate LLM step. Any caller that
    imports it must be updated to use the planner's Decision Tree instead.
    """
    import kiso.brain as brain
    assert not hasattr(brain, "run_classifier"), (
        "kiso.brain.run_classifier must be retired in M1620 — "
        "the planner's Decision Tree is the single source of routing"
    )


def test_chat_kb_preflight_fallback_is_gone():
    """M1579d's preflight broker pause is gone.

    The chat_kb fast path that this preflight fed into is deleted, so the
    preflight has nothing to gate. Pattern C (preflight false negatives on
    teach intents) disappears by design.
    """
    from kiso.worker import loop
    assert not hasattr(loop, "_chat_kb_preflight_fallback"), (
        "_chat_kb_preflight_fallback must be retired in M1620 — "
        "the chat_kb fast path it fed into is gone"
    )


def test_fast_path_chat_is_gone():
    """``_fast_path_chat`` (the no-planner fast path for ``chat`` /
    ``chat_kb`` classifications) is deleted. Every message now goes
    through the planner.
    """
    from kiso.worker import loop
    assert not hasattr(loop, "_fast_path_chat"), (
        "_fast_path_chat must be retired in M1620 — every message "
        "goes through briefer → planner → execute"
    )


def test_chat_kb_fallback_messages_are_gone():
    """The deterministic fallback messages emitted on chat_kb preflight
    empty (``_CHAT_KB_FALLBACK_MSGS``) are gone. Their function (graceful
    "I don't have this in my KB" message) is now produced by the planner
    via Decision Tree branch 5 (kb_answer) or branch 2 (awaits_input).
    """
    from kiso.worker import loop
    assert not hasattr(loop, "_CHAT_KB_FALLBACK_MSGS"), (
        "_CHAT_KB_FALLBACK_MSGS must be retired in M1620 — the planner "
        "produces the equivalent msg via Decision Tree branches 2/5"
    )


# ---------------------------------------------------------------------------
# Prompt-side deletions
# ---------------------------------------------------------------------------


def test_classifier_md_is_deleted():
    """``kiso/roles/classifier.md`` no longer exists. The role file
    retired together with the function that loaded it.
    """
    classifier_md = _ROLES_DIR / "classifier.md"
    assert not classifier_md.exists(), (
        "kiso/roles/classifier.md must be deleted in M1620 — "
        "the classifier role no longer exists"
    )


def test_investigate_module_marker_is_deleted():
    """The opt-in ``<!-- MODULE: investigate -->`` marker in planner.md
    is removed. M1619 moved its content into the Decision Tree
    (branch 6); the marker has nothing left to load.
    """
    planner_md = (_ROLES_DIR / "planner.md").read_text()
    assert "<!-- MODULE: investigate -->" not in planner_md, (
        "the legacy investigate module marker must be removed in M1620 — "
        "Decision Tree branch 6 owns the diagnostic-intent contract now"
    )


# ---------------------------------------------------------------------------
# Pipeline-shape lock
# ---------------------------------------------------------------------------


def test_process_message_has_no_classifier_dispatch():
    """The ``_process_message`` source no longer references the
    classifier dispatch tokens (``msg_class``, ``run_classifier``,
    ``chat_kb``, ``fast_path``). The pipeline is briefer→planner→execute
    unconditionally.
    """
    from kiso.worker import loop
    import inspect
    src = inspect.getsource(loop._process_message).lower()
    assert "run_classifier" not in src, (
        "_process_message must not call run_classifier"
    )
    assert "msg_class" not in src, (
        "_process_message must not branch on msg_class — Decision Tree decides"
    )
    assert "_fast_path_chat" not in src, (
        "_process_message must not invoke any fast-path chat skip"
    )
    assert "_chat_kb_preflight_fallback" not in src, (
        "_process_message must not invoke the chat_kb preflight"
    )
