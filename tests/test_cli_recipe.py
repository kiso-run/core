"""M773: Tests for CLI recipe commands (recipe management)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.recipe import (
    RECIPES_DIR,
    _recipe_install,
    _recipe_list,
    _recipe_remove,
    run_recipe_command,
)


_VALID_RECIPE = """\
---
name: data-analyst
summary: Guides planner for data analysis tasks
---

When the user asks about data analysis:
- Prefer pandas for tabular data
"""


class _FakeArgs:
    """Minimal args namespace for testing."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestRecipeList:
    def test_list_empty(self, tmp_path, capsys):
        with patch("cli.recipe.RECIPES_DIR", tmp_path):
            _recipe_list()
        assert "No recipes installed" in capsys.readouterr().out

    def test_list_with_recipes(self, tmp_path, capsys):
        (tmp_path / "data-analyst.md").write_text(_VALID_RECIPE)
        with patch("cli.recipe.RECIPES_DIR", tmp_path):
            from kiso.recipe_loader import invalidate_recipes_cache
            invalidate_recipes_cache()
            _recipe_list()
        out = capsys.readouterr().out
        assert "data-analyst" in out
        assert "Guides planner" in out


@pytest.fixture(autouse=True)
def _mock_admin():
    """M592: recipe install/remove now require admin."""
    with patch("cli.plugin_ops.require_admin"):
        yield


class TestRecipeInstall:
    def test_install_valid(self, tmp_path, capsys):
        source = tmp_path / "src" / "my-recipe.md"
        source.parent.mkdir()
        source.write_text(_VALID_RECIPE)
        dest_dir = tmp_path / "recipes"

        args = _FakeArgs(source=str(source))
        with patch("cli.recipe.RECIPES_DIR", dest_dir):
            _recipe_install(args)

        out = capsys.readouterr().out
        assert "installed" in out
        assert (dest_dir / "my-recipe.md").exists()

    def test_install_nonexistent(self, tmp_path):
        args = _FakeArgs(source=str(tmp_path / "nope.md"))
        with pytest.raises(SystemExit):
            _recipe_install(args)

    def test_install_invalid_file(self, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_text("no frontmatter here")
        args = _FakeArgs(source=str(bad))
        with pytest.raises(SystemExit):
            _recipe_install(args)

    def test_install_non_md(self, tmp_path):
        txt = tmp_path / "recipe.txt"
        txt.write_text(_VALID_RECIPE)
        args = _FakeArgs(source=str(txt))
        with pytest.raises(SystemExit):
            _recipe_install(args)

    def test_install_update_existing(self, tmp_path, capsys):
        dest_dir = tmp_path / "recipes"
        dest_dir.mkdir()
        (dest_dir / "my-recipe.md").write_text("old content")

        source = tmp_path / "my-recipe.md"
        source.write_text(_VALID_RECIPE)

        args = _FakeArgs(source=str(source))
        with patch("cli.recipe.RECIPES_DIR", dest_dir):
            _recipe_install(args)

        out = capsys.readouterr().out
        assert "updating" in out.lower()
        assert "installed" in out


class TestRecipeRemove:
    def test_remove_by_name(self, tmp_path, capsys):
        dest_dir = tmp_path / "recipes"
        dest_dir.mkdir()
        (dest_dir / "data-analyst.md").write_text(_VALID_RECIPE)

        args = _FakeArgs(name="data-analyst")
        with patch("cli.recipe.RECIPES_DIR", dest_dir):
            _recipe_remove(args)

        out = capsys.readouterr().out
        assert "removed" in out.lower()
        assert not (dest_dir / "data-analyst.md").exists()

    def test_remove_nonexistent(self, tmp_path):
        dest_dir = tmp_path / "recipes"
        dest_dir.mkdir()

        args = _FakeArgs(name="nope")
        with patch("cli.recipe.RECIPES_DIR", dest_dir):
            with pytest.raises(SystemExit):
                _recipe_remove(args)


class TestRunRecipeCommand:
    def test_no_command(self):
        args = _FakeArgs(recipe_command=None)
        with pytest.raises(SystemExit):
            run_recipe_command(args)
