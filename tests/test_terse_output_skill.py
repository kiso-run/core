"""M1548 — bundled `terse-output` fixture skill.

Exercises the messenger-only skill contract at the prompt-builder
level: a skill with a ``## Messenger`` section and no other role
sections injects its body *only* into the messenger prompt, and
that body is long enough to actually shape output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.brain.text_roles import build_messenger_messages
from kiso.brain.reviewer import build_reviewer_messages
from kiso.brain.text_roles import build_exec_translator_messages
from kiso.skill_loader import discover_skills


FIXTURE_DIR = (
    Path(__file__).parent / "functional" / "fixtures" / "skills"
    / "terse-output"
)


def test_fixture_skill_on_disk() -> None:
    skill_md = FIXTURE_DIR / "SKILL.md"
    assert skill_md.is_file(), (
        "terse-output fixture skill must live under "
        "tests/functional/fixtures/skills/terse-output/"
    )


def test_fixture_skill_has_messenger_section_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Copy fixture into a sandbox skills dir and discover it.
    sandbox = tmp_path / "skills"
    sandbox.mkdir()
    import shutil
    shutil.copytree(FIXTURE_DIR, sandbox / "terse-output")

    monkeypatch.setattr("kiso.skill_loader.KISO_DIR", tmp_path)
    from kiso.skill_loader import invalidate_skills_cache
    invalidate_skills_cache()

    skills = [s for s in discover_skills() if s.name == "terse-output"]
    assert skills, "terse-output must be discoverable"
    skill = skills[0]

    role_sections = skill.role_sections
    assert "messenger" in role_sections
    assert role_sections["messenger"].strip()
    # No other role sections.
    assert set(role_sections.keys()) == {"messenger"}


def test_messenger_prompt_carries_skill_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.conftest import make_config

    sandbox = tmp_path / "skills"
    sandbox.mkdir()
    import shutil
    shutil.copytree(FIXTURE_DIR, sandbox / "terse-output")
    monkeypatch.setattr("kiso.skill_loader.KISO_DIR", tmp_path)
    from kiso.skill_loader import invalidate_skills_cache
    invalidate_skills_cache()

    skill = next(
        s for s in discover_skills() if s.name == "terse-output"
    )
    msgs = build_messenger_messages(
        config=make_config(),
        summary="",
        facts=[],
        detail="reply politely",
        plan_outputs_text="",
        goal="do the thing",
        recent_messages=[],
        user_message="what time is it",
        briefing_context=None,
        behavior_rules=[],
        selected_skills=[skill],
    )
    text = "\n".join(m["content"] for m in msgs)
    # Canonical sentence from the skill's messenger body
    assert "one or two sentences" in text.lower()


def test_worker_and_reviewer_prompts_ignore_messenger_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.conftest import make_config

    sandbox = tmp_path / "skills"
    sandbox.mkdir()
    import shutil
    shutil.copytree(FIXTURE_DIR, sandbox / "terse-output")
    monkeypatch.setattr("kiso.skill_loader.KISO_DIR", tmp_path)
    from kiso.skill_loader import invalidate_skills_cache
    invalidate_skills_cache()

    skill = next(
        s for s in discover_skills() if s.name == "terse-output"
    )

    worker_msgs = build_exec_translator_messages(
        config=make_config(),
        detail="run a command",
        sys_env_text="Linux",
        plan_outputs_text="",
        selected_skills=[skill],
    )
    reviewer_msgs = build_reviewer_messages(
        goal="verify", detail="run a command", expect="success",
        output="ok", user_message="do it",
        selected_skills=[skill],
    )
    worker_text = "\n".join(m["content"] for m in worker_msgs).lower()
    reviewer_text = "\n".join(m["content"] for m in reviewer_msgs).lower()
    # The skill body must NOT leak into worker or reviewer prompts.
    assert "one or two sentences" not in worker_text
    assert "one or two sentences" not in reviewer_text
