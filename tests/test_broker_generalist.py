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
