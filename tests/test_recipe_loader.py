"""M773: Tests for kiso.recipe_loader — recipe discovery and parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.recipe_loader import (
    build_planner_recipe_list,
    discover_recipes,
    invalidate_recipes_cache,
    _parse_recipe_file,
)


_VALID_RECIPE = """\
---
name: data-analyst
summary: Guides planner for data analysis tasks
---

When the user asks about data analysis:
- Prefer pandas for tabular data
- Use matplotlib for charts
"""

_NO_FRONTMATTER = """\
Just a plain markdown file with no frontmatter.
"""

_MISSING_NAME = """\
---
summary: No name field
---

Instructions here.
"""

_MISSING_SUMMARY = """\
---
name: missing-summary
---

Instructions here.
"""

_EMPTY_BODY = """\
---
name: minimal
summary: Minimal recipe with no instructions
---
"""


class TestParseRecipeFile:
    def test_valid_recipe(self, tmp_path):
        f = tmp_path / "data-analyst.md"
        f.write_text(_VALID_RECIPE)
        result = _parse_recipe_file(f)
        assert result is not None
        assert result["name"] == "data-analyst"
        assert result["summary"] == "Guides planner for data analysis tasks"
        assert "pandas" in result["instructions"]
        assert result["path"] == str(f)

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text(_NO_FRONTMATTER)
        assert _parse_recipe_file(f) is None

    def test_missing_name(self, tmp_path):
        f = tmp_path / "noname.md"
        f.write_text(_MISSING_NAME)
        assert _parse_recipe_file(f) is None

    def test_missing_summary(self, tmp_path):
        f = tmp_path / "nosummary.md"
        f.write_text(_MISSING_SUMMARY)
        assert _parse_recipe_file(f) is None

    def test_empty_body(self, tmp_path):
        f = tmp_path / "minimal.md"
        f.write_text(_EMPTY_BODY)
        result = _parse_recipe_file(f)
        assert result is not None
        assert result["name"] == "minimal"
        assert result["instructions"] == ""

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nonexistent.md"
        assert _parse_recipe_file(f) is None

    def test_unclosed_frontmatter(self, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("---\nname: bad\nsummary: bad\nNo closing marker")
        assert _parse_recipe_file(f) is None


class TestDiscoverRecipes:
    def test_discovers_valid_recipes(self, tmp_path):
        (tmp_path / "recipe-a.md").write_text(_VALID_RECIPE)
        (tmp_path / "recipe-b.md").write_text(_EMPTY_BODY.replace("minimal", "recipe-b").replace(
            "Minimal recipe with no instructions", "Second recipe"))
        invalidate_recipes_cache()
        recipes = discover_recipes(tmp_path)
        assert len(recipes) == 2
        names = {r["name"] for r in recipes}
        assert "data-analyst" in names
        assert "recipe-b" in names

    def test_skips_invalid_files(self, tmp_path):
        (tmp_path / "valid.md").write_text(_VALID_RECIPE)
        (tmp_path / "invalid.md").write_text(_NO_FRONTMATTER)
        invalidate_recipes_cache()
        recipes = discover_recipes(tmp_path)
        assert len(recipes) == 1
        assert recipes[0]["name"] == "data-analyst"

    def test_empty_directory(self, tmp_path):
        invalidate_recipes_cache()
        recipes = discover_recipes(tmp_path)
        assert recipes == []

    def test_nonexistent_directory(self, tmp_path):
        invalidate_recipes_cache()
        recipes = discover_recipes(tmp_path / "nonexistent")
        assert recipes == []

    def test_ignores_non_md_files(self, tmp_path):
        (tmp_path / "valid.md").write_text(_VALID_RECIPE)
        (tmp_path / "readme.txt").write_text("not a recipe")
        (tmp_path / "script.py").write_text("pass")
        invalidate_recipes_cache()
        recipes = discover_recipes(tmp_path)
        assert len(recipes) == 1

    def test_caching(self, tmp_path):
        (tmp_path / "recipe.md").write_text(_VALID_RECIPE)
        invalidate_recipes_cache()
        first = discover_recipes(tmp_path)
        assert len(first) == 1
        # Add another file — cache should return old result
        (tmp_path / "new.md").write_text(_EMPTY_BODY)
        second = discover_recipes(tmp_path)
        assert len(second) == 1  # still cached
        # Invalidate and re-discover
        invalidate_recipes_cache()
        third = discover_recipes(tmp_path)
        assert len(third) == 2

    def test_sorted_by_filename(self, tmp_path):
        (tmp_path / "z-recipe.md").write_text(
            "---\nname: z-last\nsummary: Last\n---\nBody")
        (tmp_path / "a-recipe.md").write_text(
            "---\nname: a-first\nsummary: First\n---\nBody")
        invalidate_recipes_cache()
        recipes = discover_recipes(tmp_path)
        assert recipes[0]["name"] == "a-first"
        assert recipes[1]["name"] == "z-last"


class TestBuildPlannerRecipeList:
    def test_empty_list(self):
        assert build_planner_recipe_list([]) == ""

    def test_single_recipe(self):
        recipes = [{"name": "analyst", "summary": "Data analysis", "instructions": "Use pandas."}]
        result = build_planner_recipe_list(recipes)
        assert "- analyst — Data analysis" in result
        assert "  Use pandas." in result

    def test_multiple_recipes(self):
        recipes = [
            {"name": "analyst", "summary": "Data analysis", "instructions": "Use pandas."},
            {"name": "writer", "summary": "Writing style", "instructions": "Be concise."},
        ]
        result = build_planner_recipe_list(recipes)
        assert "- analyst — Data analysis" in result
        assert "- writer — Writing style" in result

    def test_no_instructions(self):
        recipes = [{"name": "minimal", "summary": "Minimal", "instructions": ""}]
        result = build_planner_recipe_list(recipes)
        assert result == "- minimal — Minimal"
