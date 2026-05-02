"""M1589 — Broker generalist locks (anti-overfitting, decision 6).

The broker model must work for ANY capability name, not just the
ones the prompt happens to mention. These checks catch two drift
vectors:

1. **Prompt overfitting**: a future edit re-introduces a hardcoded
   MCP name (`perplexity`, `transcriber-mcp`, `kiso-run/...`) into
   the planner / classifier role. M1579b/c locks already cover the
   trigger names that caused real incidents; this milestone adds a
   broader sweep over the MCP catalog the briefer surfaces.

2. **Code-side overfitting**: validation rejects an mcp task because
   the server / method name doesn't match an internal allowlist. The
   broker contract is "any name is valid as long as it's in the
   catalog the briefer rendered" — never a hardcoded enum.

Live smoke (`tests/live/test_broker_generalist_live.py`) covers a
single end-to-end case with a fully-invented capability name; it is
deferred to the live tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.brain.planner import validate_plan


_PLANNER_MD = Path(__file__).resolve().parent.parent / "kiso" / "roles" / "planner.md"
_CLASSIFIER_MD = (
    Path(__file__).resolve().parent.parent / "kiso" / "roles" / "classifier.md"
)


_PROMPT_FORBIDDEN = (
    "perplexity", "sonar", "tavily", "duckduckgo", "exa-mcp",
    "transcriber-mcp", "ocr-mcp", "search-mcp",
)


@pytest.mark.parametrize("forbidden", _PROMPT_FORBIDDEN)
def test_planner_prompt_no_hardcoded_capability_names(forbidden):
    text = _PLANNER_MD.read_text().lower()
    assert forbidden.lower() not in text, (
        f"planner.md leaked a hardcoded MCP name {forbidden!r} — broker "
        f"model must stay capability-agnostic (decision 6)"
    )


@pytest.mark.parametrize("forbidden", _PROMPT_FORBIDDEN)
def test_classifier_prompt_no_hardcoded_capability_names(forbidden):
    text = _CLASSIFIER_MD.read_text().lower()
    assert forbidden.lower() not in text, (
        f"classifier.md leaked a hardcoded MCP name {forbidden!r}"
    )


def _make_msg_only_plan(**overrides) -> dict:
    base = {
        "goal": "ask user", "secrets": None,
        "tasks": [{
            "type": "msg", "detail": "Answer in English. test",
            "args": None, "expect": None,
        }],
        "extend_replan": None, "needs_install": None,
        "knowledge": None, "kb_answer": None, "awaits_input": True,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("server,method", [
    ("foo-bar-mcp", "qux"),
    ("zzz-9000", "do"),
    ("x", "y"),
    ("a-b-c-d-e", "method"),
])
def test_validate_plan_accepts_arbitrary_mcp_names(server, method):
    """Any name shape passes the validator — names come from the
    briefer's catalog, never from a static allowlist."""
    plan = _make_msg_only_plan(
        awaits_input=False,  # action plan; msg tasks reject below
        tasks=[{
            "type": "mcp", "detail": "do the thing",
            "args": {}, "expect": "ok",
            "server": server, "method": method,
        }],
    )
    errors = validate_plan(plan, installed_skills=[])
    name_related = [
        e for e in errors
        if "server" in e.lower() or "method" in e.lower() or "name" in e.lower()
    ]
    assert not name_related, (
        f"validator surfaced name-related errors for {server!r}:{method!r}: "
        f"{name_related}"
    )


# ---------------------------------------------------------------------------
# M1608 — structural backstop: plan-level fields must not appear on tasks
# ---------------------------------------------------------------------------


def test_validate_plan_rejects_task_level_awaits_input():
    """M1608 invariant 1: ``awaits_input`` is a plan-level field. Putting
    it on a task is invalid — the worker reads the plan-level flag, so
    a task-level value is silently ignored, leaving the plan as a normal
    action plan instead of a broker pause. The validator must reject the
    misplacement with a clear error message so the LLM learns the right
    schema on the next attempt.

    Plan shape: action plan that would otherwise validate (exec + msg
    with substantive content, plan-level awaits_input=False). The single
    invalid thing is the ``awaits_input`` field on a task. Without the
    backstop the validator returns no errors; with the backstop it
    returns at least one error mentioning "task" + "awaits_input".
    """
    plan = _make_msg_only_plan(
        awaits_input=False,
        tasks=[
            {
                "type": "exec",
                "detail": "list files in /tmp",
                "args": None, "expect": "directory listing",
            },
            {
                "type": "msg",
                "detail": "Answer in English. Files listed successfully.",
                "args": None, "expect": None,
                "awaits_input": True,  # invalid — plan-level only
            },
        ],
    )
    errors = validate_plan(plan, installed_skills=[])
    task_level_errors = [
        e for e in errors
        if "awaits_input" in e.lower() and "task" in e.lower()
        and ("plan-level" in e.lower() or "plan level" in e.lower()
             or "not a task" in e.lower())
    ]
    assert task_level_errors, (
        f"validator must reject task-level awaits_input with a message "
        f"that names the misplacement; got errors {errors!r}"
    )


def test_validate_plan_accepts_plan_level_awaits_input_msg_only():
    """M1608 baseline: a msg-only plan with plan-level ``awaits_input``
    is the correct shape and the validator accepts it. This guards
    against the previous test's fix accidentally breaking the canonical
    ask-first plan shape.
    """
    plan = _make_msg_only_plan(awaits_input=True)
    errors = validate_plan(plan, installed_skills=[])
    awaits_errors = [e for e in errors if "awaits_input" in e.lower()]
    assert awaits_errors == [], (
        f"plan-level awaits_input on a msg-only plan is the canonical "
        f"shape and must validate; got {errors!r}"
    )


# ---------------------------------------------------------------------------
# M1608 — prompt invariants present (abstract — no specific names)
# ---------------------------------------------------------------------------


def test_planner_prompt_states_awaits_input_is_plan_level():
    """The planner prompt must explicitly state that ``awaits_input`` is
    a plan-level field, not a task-level field. The wording is what
    teaches the LLM the schema; the validator backstop only catches
    after-the-fact mistakes.
    """
    text = _PLANNER_MD.read_text().lower()
    assert "awaits_input" in text and "plan-level" in text, (
        "planner.md must state that awaits_input is plan-level — the "
        "broker pause invariant relies on this distinction"
    )


def test_planner_prompt_states_install_proposal_first_for_unknown_sources():
    """The planner prompt must state that an install request from an
    unknown / non-tier1 source is a ``needs_install`` msg-only proposal,
    NOT a direct exec install. This is the invariant that protects
    against the planner inventing fallback install commands or skipping
    the trust gate.
    """
    text = _PLANNER_MD.read_text().lower()
    assert "needs_install" in text, (
        "planner.md must reference needs_install as the proposal field"
    )
    # The phrase that teaches the rule must appear — abstract wording,
    # no specific source name. We require the presence of a sentence
    # that pairs "before approval" with a forbidden behavior; the exact
    # phrasing is left to prompt-craft, but the lemma must be there.
    assert "before approval" in text or "before the user approves" in text, (
        "planner.md must forbid exec installs before user approval"
    )
