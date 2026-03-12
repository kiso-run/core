"""M452: Integration test — MD skill discovery through briefer flow.

End-to-end test: temp .md skills → discover → context_pool → briefer → planner messages.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiso.brain import build_planner_messages
from kiso.config import Config, Provider
from kiso.skill_loader import discover_md_skills, invalidate_md_skills_cache
from kiso.store import create_session, init_db


_SKILL_DATA_ANALYST = """\
---
name: data-analyst
summary: Guides planner for data analysis tasks
---

When the user asks about data analysis:
- Prefer pandas for tabular data
- Use matplotlib for charts
"""

_SKILL_CODE_REVIEW = """\
---
name: code-reviewer
summary: Code review best practices
---

When reviewing code:
- Check for error handling
- Verify test coverage
"""


def _full_settings(**overrides) -> dict:
    from kiso.config import SETTINGS_DEFAULTS
    s = dict(SETTINGS_DEFAULTS)
    s.setdefault("classifier_timeout", 30)
    s.update(overrides)
    return s


def _full_models(**overrides) -> dict:
    from kiso.config import MODEL_DEFAULTS
    m = dict(MODEL_DEFAULTS)
    m.update(overrides)
    return m


def _config(briefer_enabled=True) -> Config:
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=_full_models(),
        settings=_full_settings(context_messages=3, briefer_enabled=briefer_enabled),
        raw={},
    )


def _briefing(context="", **kw) -> dict:
    return {
        "modules": kw.get("modules", []),
        "tools": kw.get("tools", []),
        "context": context,
        "output_indices": kw.get("output_indices", []),
        "relevant_tags": kw.get("relevant_tags", []),
        "relevant_entities": kw.get("relevant_entities", []),
    }


@pytest.fixture()
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    await create_session(conn, "sess1")
    yield conn
    await conn.close()


class TestMdSkillEndToEnd:
    """Integration: .md files → discovery → briefer context pool → planner."""

    async def test_discover_from_temp_dir(self, tmp_path):
        """Skills in a temp dir are discovered and parsed correctly."""
        (tmp_path / "data-analyst.md").write_text(_SKILL_DATA_ANALYST)
        (tmp_path / "code-reviewer.md").write_text(_SKILL_CODE_REVIEW)

        invalidate_md_skills_cache()
        skills = discover_md_skills(tmp_path)

        assert len(skills) == 2
        names = {s["name"] for s in skills}
        assert names == {"code-reviewer", "data-analyst"}
        analyst = next(s for s in skills if s["name"] == "data-analyst")
        assert "pandas" in analyst["instructions"]

    async def test_skills_reach_briefer_context_pool(self, db, tmp_path):
        """When MD skills exist, briefer receives them in context pool."""
        (tmp_path / "data-analyst.md").write_text(_SKILL_DATA_ANALYST)
        invalidate_md_skills_cache()

        captured_messages = []

        async def _capturing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps(_briefing(
                    context="User wants data analysis help."))
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_capturing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_md_skills",
                   side_effect=lambda *a, **k: discover_md_skills(tmp_path)):
            await build_planner_messages(
                db, _config(), "sess1", "user", "analyze this CSV",
            )

        briefer_input = captured_messages[1]["content"]
        assert "Available Skills" in briefer_input
        assert "data-analyst" in briefer_input
        assert "Guides planner for data analysis tasks" in briefer_input

    async def test_multiple_skills_all_reach_briefer(self, db, tmp_path):
        """Multiple MD skills all appear in briefer context pool."""
        (tmp_path / "data-analyst.md").write_text(_SKILL_DATA_ANALYST)
        (tmp_path / "code-reviewer.md").write_text(_SKILL_CODE_REVIEW)
        invalidate_md_skills_cache()

        captured_messages = []

        async def _capturing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps(_briefing(context="Multi-skill context."))
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_capturing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_md_skills",
                   side_effect=lambda *a, **k: discover_md_skills(tmp_path)):
            await build_planner_messages(
                db, _config(), "sess1", "user", "review this code",
            )

        briefer_input = captured_messages[1]["content"]
        assert "data-analyst" in briefer_input
        assert "code-reviewer" in briefer_input

    async def test_briefer_passes_skill_to_planner(self, db, tmp_path):
        """When briefer includes skill content in context, planner sees it."""
        (tmp_path / "data-analyst.md").write_text(_SKILL_DATA_ANALYST)
        invalidate_md_skills_cache()

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(_briefing(
                    context="User needs data analysis.\n\n"
                            "## Skills\n- data-analyst: Use pandas for tabular data."))
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_md_skills",
                   side_effect=lambda *a, **k: discover_md_skills(tmp_path)):
            msgs, _, _ = await build_planner_messages(
                db, _config(), "sess1", "user", "analyze this dataset",
            )

        user_content = msgs[1]["content"]
        assert "pandas" in user_content

    async def test_no_skills_no_section_in_briefer(self, db, tmp_path):
        """When no MD skills, briefer gets no skills section."""
        invalidate_md_skills_cache()

        captured_messages = []

        async def _capturing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps(_briefing(context="No skills needed."))
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_capturing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_md_skills", return_value=[]):
            await build_planner_messages(
                db, _config(), "sess1", "user", "hello",
            )

        briefer_input = captured_messages[1]["content"]
        assert "Available Skills" not in briefer_input

    async def test_skills_with_briefer_disabled(self, db, tmp_path):
        """When briefer is disabled, skills still appear in planner context (fallback path)."""
        (tmp_path / "data-analyst.md").write_text(_SKILL_DATA_ANALYST)
        invalidate_md_skills_cache()

        async def _fake_llm(cfg, role, messages, **kw):
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]), \
             patch("kiso.brain.discover_md_skills",
                   side_effect=lambda *a, **k: discover_md_skills(tmp_path)):
            msgs, _, _ = await build_planner_messages(
                db, _config(briefer_enabled=False), "sess1", "user",
                "analyze this data",
            )

        # In fallback path, all context pool items are included directly
        user_content = msgs[1]["content"]
        assert "data-analyst" in user_content
