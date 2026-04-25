"""Discovery + content invariants for the bundled
``voice-message-receiver`` skill at
``plugins/skill-voice-message-receiver/``.

The skill is shipped in-tree as a plugin; users install it with
``kiso skill install --from-url ./plugins/skill-voice-message-receiver``.
These tests pin its surface so an accidental edit to the SKILL.md
front-matter or to the planner section can't silently break the
voice-message → transcribe → replan flow.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kiso.skill_loader import discover_skills, parse_skill_file


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILL_PATH = _REPO_ROOT / "plugins" / "voice-message-receiver-skill"


@pytest.fixture(scope="module")
def skill():
    """Parse the bundled SKILL.md once."""
    skill_md = _SKILL_PATH / "SKILL.md"
    assert skill_md.is_file(), (
        f"voice-message-receiver SKILL.md missing at {skill_md}"
    )
    return parse_skill_file(skill_md, bundled_root=_SKILL_PATH)


class TestFrontmatter:
    def test_name_is_canonical(self, skill):
        assert skill.name == "voice-message-receiver"

    def test_description_present(self, skill):
        assert skill.description
        assert "transcrib" in skill.description.lower()

    def test_when_to_use_mentions_audio_extensions(self, skill):
        wt = (skill.when_to_use or "").lower()
        for ext in ("mp3", "m4a", "wav", "ogg"):
            assert ext in wt, f"when_to_use must mention {ext}"

    def test_activation_hints_target_audio_voice(self, skill):
        hints = skill.activation_hints or {}
        applies = set(hints.get("applies_to") or [])
        assert "audio" in applies or "voice" in applies, (
            f"activation_hints.applies_to must include audio or voice; "
            f"got {applies}"
        )


class TestPlannerGuidance:
    def test_planner_section_present(self, skill):
        assert "planner" in skill.role_sections, (
            "skill must have a `## Planner` section so the planner "
            "prompt builder picks it up"
        )

    def test_planner_section_routes_to_transcriber(self, skill):
        body = skill.role_sections["planner"].lower()
        assert "kiso-transcriber" in body
        assert "transcribe_audio" in body

    def test_planner_section_uses_replan_loop(self, skill):
        body = skill.role_sections["planner"].lower()
        assert "replan" in body, (
            "planner section must instruct a replan after transcription "
            "so the next planner pass sees the transcript"
        )

    def test_planner_section_lists_audio_extensions(self, skill):
        body = skill.role_sections["planner"].lower()
        # Subset of the supported extensions — full list is in the
        # skill body; smoke-check the most common ones.
        for ext in (".mp3", ".m4a", ".wav", ".ogg"):
            assert ext in body, f"planner section must list {ext}"

    def test_planner_section_forbids_one_shot_plan(self, skill):
        """Critical safety rule: never transcribe + act in one plan."""
        body = skill.role_sections["planner"].lower()
        assert "two stages" in body or "two stage" in body or \
               "never put the transcription" in body or \
               "never transcribe" in body, (
            "planner section must explicitly forbid combining the "
            "transcribe call with the follow-up action in one plan"
        )


class TestSkillIsDiscoverable:
    def test_discover_finds_skill(self, tmp_path):
        """When the skill folder is dropped into a skills dir,
        discover_skills must pick it up by name."""
        # Copy the bundled skill into a temp skills dir to simulate
        # what `kiso skill install --from-url ./plugins/skill-voice-message-receiver`
        # ends up doing.
        import shutil
        target = tmp_path / "voice-message-receiver"
        shutil.copytree(_SKILL_PATH, target)
        skills = discover_skills(skills_dir=tmp_path)
        names = [s.name for s in skills]
        assert "voice-message-receiver" in names
