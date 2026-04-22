"""Tests for behavior injection into the messenger prompt.

Business requirement: behaviors added via ``kiso behavior add`` are
account-level output-style directives — they must reach the
messenger prompt. Skills with a ``## Messenger`` section cover the
per-domain layer (landed in M1520); behaviors cover the
account-level layer. Both coexist under clearly separate headers.
"""

from __future__ import annotations

from kiso.brain.text_roles import build_messenger_messages
from kiso.config import Config, Provider


def _config() -> Config:
    return Config(
        tokens={}, providers={"openrouter": Provider(base_url="x")},
        users={}, models={},
        settings={"bot_name": "Kiso", "bot_persona": "a friendly assistant"},
        raw={},
    )


class TestBehaviorInjection:
    def test_behavior_rules_reach_prompt(self):
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
            behavior_rules=["respond in one sentence", "no emoji"],
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "respond in one sentence" in joined
        assert "no emoji" in joined

    def test_behavior_rules_under_dedicated_header(self):
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
            behavior_rules=["always English"],
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "Behavior" in joined

    def test_empty_behavior_rules_omits_section(self):
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
            behavior_rules=None,
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "Behavior Guidelines" not in joined

    def test_behaviors_and_messenger_skill_section_coexist(self):
        """Two-layer output-style model: account behaviors +
        per-domain skill messenger sections."""
        from kiso.skill_loader import Skill

        skill = Skill(
            name="terse",
            description="Be brief.",
            role_sections={"messenger": "- one sentence"},
        )
        msgs = build_messenger_messages(
            _config(), summary="", facts=[], detail="Answer", goal="",
            behavior_rules=["no emoji"],
            selected_skills=[skill],
        )
        joined = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "no emoji" in joined
        assert "one sentence" in joined
        assert "Behavior" in joined
        assert "Skills" in joined
