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


# ---------------------------------------------------------------------------
# M1609 — MCP-preference invariant (abstract — no server names)
# ---------------------------------------------------------------------------


def test_planner_prompt_prefers_installed_mcp_over_inline_exec():
    """When the briefer lists an MCP method whose declared capability
    covers the user's intent, the planner must use the MCP rather than
    reimplement the capability via inline ``exec``. The rule must be
    present in the prompt as a directive sentence that explicitly
    pairs "MCP" with "rather than" / "not" / "instead of" + "exec" —
    a generic "Prefer MCP for remote APIs" sentence elsewhere in the
    prompt is NOT enough, because V4-Flash skips it when the user's
    intent feels scriptable.

    No specific server / method / capability name (`kiso-search`,
    `web search`, etc.) may appear in the new directive.
    """
    text = _PLANNER_MD.read_text().lower()
    # Look for the directive in compact form: a window of ~200 chars
    # that contains BOTH "mcp" and "exec" AND one of the rejection
    # phrases ("rather than" / "instead of" / "not reimplement" / "not
    # via exec"). This is what teaches the LLM "use the MCP, don't
    # script the same thing in exec".
    import re as _re
    _RE = _re.compile(
        r"mcp[^\n]{0,200}(rather than|instead of|not reimplement|not via exec|never reimplement)[^\n]{0,200}exec"
        r"|"
        r"exec[^\n]{0,200}(rather than|instead of)[^\n]{0,200}mcp",
        _re.DOTALL,
    )
    assert _RE.search(text), (
        "planner.md must contain a directive that pairs 'MCP' with a "
        "rejection of inline exec ('rather than', 'instead of', 'never "
        "reimplement') for the same capability — generic 'Prefer MCP' "
        "wording is not directive enough"
    )


def test_planner_prompt_allows_exec_fallback_when_mcp_unavailable():
    """The MCP-preference rule must explicitly allow ``exec`` fallback
    when the MCP has failed or is broken in this session — otherwise
    M1585's recovery flow regresses (the planner would refuse to fall
    back even after the MCP demonstrably can't serve the request).
    The fallback wording is constrained: the always-loaded
    `skills_and_mcp` module must NOT use the word "unhealthy" — that
    term belongs to the opt-in `mcp_recovery` module (cf.
    `tests/test_briefer.py::TestBrieferScenarios`).
    """
    text = _PLANNER_MD.read_text().lower()
    assert "fail" in text or "broken" in text, (
        "planner.md must keep the exec-fallback escape hatch for the "
        "MCP-recovery / failed-server case (M1585 invariant) using "
        "wording that does not collide with the opt-in mcp_recovery "
        "module ('unhealthy')"
    )
