"""Recipe management CLI commands.

Recipes are lightweight .md files with YAML frontmatter that provide
planner instructions. They live in ~/.kiso/recipes/.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cli.render import die

from kiso.config import KISO_DIR
from kiso.recipe_loader import discover_recipes, invalidate_recipes_cache

RECIPES_DIR = KISO_DIR / "recipes"


def run_recipe_command(args) -> None:
    """Dispatch to the appropriate recipe subcommand."""
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "recipe_command", {
        "list": lambda _: _recipe_list(),
        "install": _recipe_install, "remove": _recipe_remove,
    }, "usage: kiso recipe {list,install,remove}")


def _recipe_list() -> None:
    """List installed recipes."""
    from cli.plugin_ops import render_aligned_list
    invalidate_recipes_cache()
    recipes = discover_recipes(RECIPES_DIR)
    if not recipes:
        print("No recipes installed.")
        return
    render_aligned_list(recipes, "name", "summary")


def _recipe_install(args) -> None:
    """Install a recipe from a local file path."""
    from cli.plugin_ops import require_admin
    require_admin()
    source = Path(args.source)
    if not source.exists():
        die(f"file not found: {source}")
    if not source.suffix == ".md":
        die("recipe file must be a .md file")

    # Validate the file has proper frontmatter
    from kiso.recipe_loader import _parse_recipe_file
    parsed = _parse_recipe_file(source)
    if parsed is None:
        die("invalid recipe file — must have YAML frontmatter with 'name' and 'summary'")

    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    dest = RECIPES_DIR / source.name
    if dest.exists():
        print(f"Recipe '{source.name}' already installed — updating.")
    shutil.copy2(source, dest)
    invalidate_recipes_cache()
    print(f"Recipe '{parsed['name']}' installed.")


def _recipe_remove(args) -> None:
    """Remove an installed recipe."""
    from cli.plugin_ops import require_admin
    require_admin()
    name = args.name
    # Try exact filename first, then match by name
    target = RECIPES_DIR / f"{name}.md"
    if not target.exists():
        target = RECIPES_DIR / name
    if not target.exists():
        # Search by recipe name in frontmatter
        invalidate_recipes_cache()
        recipes = discover_recipes(RECIPES_DIR)
        for r in recipes:
            if r["name"] == name:
                target = Path(r["path"])
                break
    if not target.exists():
        die(f"recipe '{name}' is not installed")

    target.unlink()
    invalidate_recipes_cache()
    print(f"Recipe '{name}' removed.")
