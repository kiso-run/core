"""Tests for ``## Messenger`` skill sections reaching the messenger prompt.

Business requirement: when the briefer/planner selects a skill
whose body contains a ``## Messenger`` section, that section must
reach the messenger's system-prompt context under a dedicated
heading. Skills without a ``## Messenger`` section contribute
nothing to the messenger (even if they have planner / worker /
reviewer sections).
"""

from __future__ import annotations

from kiso.brain.text_roles import build_messenger_messages
from kiso.config import Config, Provider
from kiso.skill_loader import Skill


def _config() -> Config:
    return Config(
        tokens={}, providers={"openrouter": Provider(base_url="x")},
        users={}, models={},
        settings={"bot_name": "Kiso", "bot_persona": "a friendly assistant"},
        raw={},
    )


def _skill(name: str, role_sections: dict[str, str]) -> Skill:
    return Skill(
        name=name,
        description="test",
        role_sections=role_sections,
    )


class TestMessengerSkillInjection:
    def test_messenger_section_reaches_prompt(self):
        skill = _skill("terse", {"messenger": "- no greetings\n- terse."})
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
            selected_skills=[skill],
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "terse" in joined
        assert "no greetings" in joined
        # Dedicated header makes it explicit to the model
        assert "Skills" in joined

    def test_skill_without_messenger_section_ignored(self):
        skill = _skill("python-debug", {"planner": "reproduce, isolate, fix"})
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
            selected_skills=[skill],
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "reproduce" not in joined
        # No Skills header when no skill contributes
        assert "## Skills" not in joined

    def test_multiple_messenger_sections_joined(self):
        skills = [
            _skill("terse", {"messenger": "- terse"}),
            _skill("friendly", {"messenger": "- warm tone"}),
        ]
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
            selected_skills=skills,
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "terse" in joined
        assert "warm tone" in joined

    def test_no_selected_skills_renders_same_as_before(self):
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "## Skills" not in joined
