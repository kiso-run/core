"""Retirement invariants for the ``recipe_loader`` subsystem.

Once part 2b of the wrapper/search retirement lands, these tests pin
the absence of the recipe discovery + planner integration surface.
``kiso-migrate-recipes-to-skills`` stays behind as a one-shot
transitional tool and MUST keep working against a legacy
``~/.kiso/recipes/*.md`` tree — that is the only remaining reader of
the recipe file shape.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


class TestRecipeLoaderModuleGone:

    def test_recipe_loader_module_deleted(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kiso.recipe_loader")

    def test_brain_does_not_re_export_discover_recipes(self):
        import kiso.brain as brain
        assert not hasattr(brain, "discover_recipes")
        assert not hasattr(brain, "build_planner_recipe_list")
        assert not hasattr(brain, "build_recipe_runtime_contracts_text")
        assert not hasattr(brain, "filter_recipes_for_message")


# ---------------------------------------------------------------------------
# migrate_recipes stays functional (one-shot migration tool)
# ---------------------------------------------------------------------------


class TestMigrateRecipesStillWorks:
    """The migration tool is the only consumer of the legacy recipe
    file format after part 2b. It must stay green.
    """

    def test_migrate_recipes_imports_cleanly(self):
        from kiso import migrate_recipes
        assert hasattr(migrate_recipes, "migrate_recipes")
        assert hasattr(migrate_recipes, "main")

    def test_migrate_recipes_processes_legacy_fixture(self, tmp_path):
        from kiso.migrate_recipes import migrate_recipes

        recipes_dir = tmp_path / "recipes"
        skills_dir = tmp_path / "skills"
        recipes_dir.mkdir()
        (recipes_dir / "data-analyst.md").write_text(
            "---\nname: data-analyst\nsummary: analysis guidance\n---\n"
            "Prefer structured JSON output.\n",
            encoding="utf-8",
        )
        summary = migrate_recipes(
            recipes_dir=recipes_dir, skills_dir=skills_dir,
        )
        assert summary["migrated"] == ["data-analyst"]
        assert (skills_dir / "data-analyst" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# Briefer schema no longer references recipes
# ---------------------------------------------------------------------------


class TestBrieferNoRecipesKey:

    def test_context_pool_has_no_recipes_key(self):
        import kiso.brain.common as common
        # _CONTEXT_POOL_SECTIONS used to include ("recipes", "Available
        # Recipes"); part 2b removes it.
        section_keys = {k for k, _label in common._CONTEXT_POOL_SECTIONS}
        assert "recipes" not in section_keys
        assert "_raw_recipes" not in section_keys
