"""Tests for M1540: skill role slices threaded into builder prompts.

- ``build_exec_translator_messages`` emits ``## Skills (worker guidance)``
  when passed ``selected_skills`` that have a ``## Worker`` section.
- ``build_reviewer_messages`` emits ``## Skills (reviewer heuristics)``
  after ``## Expected Outcome`` and before ``## Actual Output`` when
  passed ``selected_skills`` with a ``## Reviewer`` section.
- Planner prompt injection via ``build_planner_messages`` is covered
  in ``tests/test_brain.py::TestBuildPlannerMessages``.
"""
from __future__ import annotations

from kiso.brain.reviewer import build_reviewer_messages
from kiso.brain.text_roles import build_exec_translator_messages
from kiso.config import Config, Provider
from kiso.skill_loader import Skill

def _config() -> Config:
    # conftest provides full_models/full_settings as pytest fixtures; use
    # them indirectly via the default MODEL_DEFAULTS/SETTINGS_DEFAULTS.
    from kiso.config import MODEL_DEFAULTS, SETTINGS_DEFAULTS
    return Config(
        tokens={"cli": "t"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=dict(MODEL_DEFAULTS),
        settings=dict(SETTINGS_DEFAULTS) if isinstance(SETTINGS_DEFAULTS, dict) else {e[0]: e[1] for e in SETTINGS_DEFAULTS},
        raw={},
    )


def _skill(name: str, **role_sections: str) -> Skill:
    return Skill(
        name=name,
        description=f"{name} skill",
        role_sections=role_sections,
    )


class TestExecTranslatorSkillThreading:
    def test_worker_section_injected(self):
        skill = _skill("git-triage", worker="Prefer `git log --oneline -20` first")
        msgs = build_exec_translator_messages(
            _config(),
            detail="investigate a regression",
            sys_env_text="linux x86_64",
            selected_skills=[skill],
        )
        content = msgs[1]["content"]
        assert "## Skills (worker guidance)" in content
        assert "### git-triage" in content
        assert "Prefer `git log --oneline -20` first" in content

    def test_skill_without_worker_section_contributes_nothing(self):
        # A skill with only a planner section → no worker output.
        skill = _skill("planner-only", planner="plan very carefully")
        msgs = build_exec_translator_messages(
            _config(),
            detail="run tests",
            sys_env_text="",
            selected_skills=[skill],
        )
        content = msgs[1]["content"]
        assert "## Skills (worker guidance)" not in content

    def test_no_skills_no_section(self):
        msgs = build_exec_translator_messages(
            _config(),
            detail="ls",
            sys_env_text="",
        )
        content = msgs[1]["content"]
        assert "Skills" not in content


class TestReviewerSkillThreading:
    def test_reviewer_section_appears_between_expect_and_actual(self):
        skill = _skill(
            "test-review",
            reviewer="Look for missing error messages in stderr",
        )
        msgs = build_reviewer_messages(
            goal="fix the flaky test",
            detail="run pytest",
            expect="exit 0",
            output="OK",
            user_message="please fix",
            selected_skills=[skill],
        )
        content = msgs[1]["content"]
        exp_idx = content.index("## Expected Outcome")
        skills_idx = content.index("## Skills (reviewer heuristics)")
        actual_idx = content.index("## Actual Output")
        assert exp_idx < skills_idx < actual_idx
        assert "Look for missing error messages" in content

    def test_skill_without_reviewer_section_omitted(self):
        skill = _skill("planner-only", planner="plan carefully")
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="u",
            selected_skills=[skill],
        )
        content = msgs[1]["content"]
        assert "## Skills (reviewer heuristics)" not in content

    def test_expect_remains_primary_criterion(self):
        # The reviewer prompt keeps ``## Expected Outcome`` as its own
        # section even when a skill is present — the skill supplements,
        # it doesn't replace.
        skill = _skill("x", reviewer="anything")
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="files must exist at /tmp/out",
            output="", user_message="", selected_skills=[skill],
        )
        content = msgs[1]["content"]
        assert "files must exist at /tmp/out" in content
        assert "## Expected Outcome" in content
