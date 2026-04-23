"""Full-skill role-section integration (M1540 follow-up).

Locks the role-scoping invariant: a skill with **all four** role
sections (``## Planner``, ``## Worker``, ``## Reviewer``,
``## Messenger``) has exactly one role body reach each role's
prompt. A ``## Worker`` body must not leak into the reviewer prompt
and vice versa.

Pure prompt-builder integration. The planner builder is covered in
``test_skill_role_threading.py`` (async path with DB); this file
pins the three synchronous builders: worker, reviewer, messenger.
"""

from __future__ import annotations

from kiso.brain.reviewer import build_reviewer_messages
from kiso.brain.text_roles import (
    build_exec_translator_messages,
    build_messenger_messages,
)
from kiso.skill_loader import Skill

from tests.conftest import make_config


PLANNER_MARK = "PLANNER-MARK-UNIQUE-0dc44"
WORKER_MARK = "WORKER-MARK-UNIQUE-1e39b"
REVIEWER_MARK = "REVIEWER-MARK-UNIQUE-2fd57"
MESSENGER_MARK = "MESSENGER-MARK-UNIQUE-3ae21"


def _fixture_skill() -> Skill:
    return Skill(
        name="full-role-skill",
        description="covers all four roles",
        body=(
            f"## Planner\n\n{PLANNER_MARK}\n\n"
            f"## Worker\n\n{WORKER_MARK}\n\n"
            f"## Reviewer\n\n{REVIEWER_MARK}\n\n"
            f"## Messenger\n\n{MESSENGER_MARK}\n"
        ),
        role_sections={
            "planner": PLANNER_MARK,
            "worker": WORKER_MARK,
            "reviewer": REVIEWER_MARK,
            "messenger": MESSENGER_MARK,
        },
    )


def _minimal_config():
    return make_config()


def _joined(messages) -> str:
    return "\n".join(m["content"] for m in messages)


def test_worker_prompt_has_only_worker_mark() -> None:
    skill = _fixture_skill()
    msgs = build_exec_translator_messages(
        config=_minimal_config(),
        detail="run the tests",
        sys_env_text="Linux",
        plan_outputs_text="",
        selected_skills=[skill],
    )
    text = _joined(msgs)
    assert WORKER_MARK in text
    assert PLANNER_MARK not in text
    assert REVIEWER_MARK not in text
    assert MESSENGER_MARK not in text


def test_reviewer_prompt_has_only_reviewer_mark() -> None:
    skill = _fixture_skill()
    msgs = build_reviewer_messages(
        goal="verify the thing",
        detail="run the tests",
        expect="tests pass",
        output="ok",
        user_message="do the thing",
        selected_skills=[skill],
    )
    text = _joined(msgs)
    assert REVIEWER_MARK in text
    assert PLANNER_MARK not in text
    assert WORKER_MARK not in text
    assert MESSENGER_MARK not in text


def test_messenger_prompt_has_only_messenger_mark() -> None:
    skill = _fixture_skill()
    msgs = build_messenger_messages(
        config=_minimal_config(),
        summary="",
        facts=[],
        detail="reply politely",
        plan_outputs_text="",
        goal="do the thing",
        recent_messages=[],
        user_message="do the thing",
        briefing_context=None,
        behavior_rules=[],
        selected_skills=[skill],
    )
    text = _joined(msgs)
    assert MESSENGER_MARK in text
    assert PLANNER_MARK not in text
    assert WORKER_MARK not in text
    assert REVIEWER_MARK not in text
