"""Integration test — recipe discovery through briefer flow.

End-to-end test: temp .md recipes → discover → context_pool → briefer → planner messages.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiso.brain import build_planner_messages
from kiso.config import Config, Provider
from kiso.recipe_loader import discover_recipes, invalidate_recipes_cache
from kiso.store import create_session, init_db
from tests.conftest import full_settings, full_models


_RECIPE_DATA_ANALYST = """\
---
name: data-analyst
summary: Guides planner for data analysis tasks
---

When the user asks about data analysis:
- Prefer pandas for tabular data
- Use matplotlib for charts
"""

_RECIPE_CODE_REVIEW = """\
---
name: code-reviewer
summary: Code review best practices
applies_to: code, review
---

When reviewing code:
- Check for error handling
- Verify test coverage
"""

_RECIPE_ENV_REPORT = """\
---
name: env-report
summary: Format environment reports as structured data
applies_to: environment, env, report
excludes: marketing
---

Return key-value output.
"""


def _config(briefer_enabled=True) -> Config:
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=full_models(),
        settings=full_settings(context_messages=3, briefer_enabled=briefer_enabled),
        raw={},
    )


@pytest.fixture()
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    await create_session(conn, "sess1")
    yield conn
    await conn.close()


class TestRecipeEndToEnd:
    """Integration: .md files → discovery → briefer context pool → planner."""

    async def test_discover_from_temp_dir(self, tmp_path):
        """Recipes in a temp dir are discovered and parsed correctly."""
        (tmp_path / "data-analyst.md").write_text(_RECIPE_DATA_ANALYST)
        (tmp_path / "code-reviewer.md").write_text(_RECIPE_CODE_REVIEW)

        invalidate_recipes_cache()
        recipes = discover_recipes(tmp_path)

        assert len(recipes) == 2
        names = {r["name"] for r in recipes}
        assert names == {"code-reviewer", "data-analyst"}
        analyst = next(r for r in recipes if r["name"] == "data-analyst")
        assert "pandas" in analyst["instructions"]

    async def test_recipes_with_briefer_disabled(self, db, tmp_path):
        """When briefer is disabled, recipes still appear in planner context (fallback path)."""
        (tmp_path / "data-analyst.md").write_text(_RECIPE_DATA_ANALYST)
        invalidate_recipes_cache()

        async def _fake_llm(cfg, role, messages, **kw):
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_wrappers", return_value=[]), \
             patch("kiso.brain.discover_recipes",
                   side_effect=lambda *a, **k: discover_recipes(tmp_path)):
            msgs, _, _ = await build_planner_messages(
                db, _config(briefer_enabled=False), "sess1", "user",
                "analyze this data",
            )

        # In fallback path, all context pool items are included directly
        user_content = msgs[1]["content"]
        assert "data-analyst" in user_content

    async def test_metadata_prefilters_recipes_before_briefer(self, db, tmp_path):
        """Static metadata should remove clearly irrelevant recipes before briefer selection."""
        (tmp_path / "env-report.md").write_text(_RECIPE_ENV_REPORT)
        (tmp_path / "code-reviewer.md").write_text(_RECIPE_CODE_REVIEW)
        invalidate_recipes_cache()

        captured_messages = []

        async def _capturing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return '{"modules":[],"wrappers":[],"exclude_recipes":[],"context":"","output_indices":[],"relevant_tags":[],"relevant_entities":[]}'
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_capturing_llm), \
             patch("kiso.brain.discover_wrappers", return_value=[]), \
             patch("kiso.brain.discover_recipes",
                   side_effect=lambda *a, **k: discover_recipes(tmp_path)):
            await build_planner_messages(
                db, _config(briefer_enabled=True), "sess1", "user",
                "fammi un report delle variabili d'ambiente del sistema",
            )

        briefer_input = captured_messages[1]["content"]
        assert "env-report" in briefer_input
        assert "code-reviewer" not in briefer_input
