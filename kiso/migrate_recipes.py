"""``kiso-migrate-recipes-to-skills`` — convert legacy recipes to skills.

Reads every ``~/.kiso/recipes/*.md`` file, and for each one writes a
matching ``~/.kiso/skills/<name>/SKILL.md`` under the Kiso Skill
Profile:

- Standard frontmatter fields (``name``, ``description``) copied from
  the recipe's ``name``/``summary``.
- Kiso extension ``activation_hints`` mirrors recipe
  ``applies_to``/``excludes``.
- Recipe body → ``## Planner`` role section.
- When ``_infer_runtime_contract`` detects an output-shape contract
  (``json_object`` or ``key_value``), a synthetic ``## Worker`` section
  is added mirroring the legacy ``build_recipe_runtime_contracts_text``
  wording.

Idempotent: existing ``SKILL.md`` files under ``<name>/`` are left
untouched. A summary of migrated / skipped names is printed at the
end.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.recipe_loader import discover_recipes


def migrate_recipes(
    *,
    recipes_dir: Path | None = None,
    skills_dir: Path | None = None,
    overwrite: bool = False,
) -> dict:
    """Run the migration. Returns a summary dict."""
    recipes_root = recipes_dir or (KISO_DIR / "recipes")
    skills_root = skills_dir or (KISO_DIR / "skills")

    summary = {
        "migrated": [],
        "skipped_existing": [],
        "source_missing": False,
    }
    if not recipes_root.is_dir():
        summary["source_missing"] = True
        return summary

    skills_root.mkdir(parents=True, exist_ok=True)
    recipes = discover_recipes(recipes_root)

    for recipe in recipes:
        name = recipe["name"]
        target_dir = skills_root / name
        target_file = target_dir / "SKILL.md"
        if target_file.exists() and not overwrite:
            summary["skipped_existing"].append(name)
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        target_file.write_text(_render_skill_md(recipe), encoding="utf-8")
        summary["migrated"].append(name)

    return summary


def _render_skill_md(recipe: dict) -> str:
    name = recipe["name"]
    description = recipe["summary"]
    applies_to = recipe.get("applies_to") or []
    excludes = recipe.get("excludes") or []
    body = recipe.get("instructions", "").strip()
    contract = recipe.get("runtime_contract") or {}

    lines: list[str] = ["---", f"name: {name}", f"description: {description}"]
    if applies_to or excludes:
        lines.append("activation_hints:")
        if applies_to:
            lines.append("  applies_to:")
            lines.extend(f"    - {sel}" for sel in applies_to)
        if excludes:
            lines.append("  excludes:")
            lines.extend(f"    - {sel}" for sel in excludes)
    lines.append("---")
    lines.append("")
    lines.append("## Planner")
    lines.append("")
    lines.append(body if body else "_(no body)_")

    worker_section = _synthesize_worker_section(contract)
    if worker_section:
        lines.append("")
        lines.append("## Worker")
        lines.append("")
        lines.append(worker_section)

    return "\n".join(lines).rstrip() + "\n"


def _synthesize_worker_section(contract: dict) -> str:
    """Match the legacy build_recipe_runtime_contracts_text wording."""
    kind = contract.get("kind") if contract else None
    if kind == "json_object":
        return "Prefer a valid JSON object when producing structured output."
    if kind == "key_value":
        return (
            "Emit the result as ``key: value`` lines — one per line, no "
            "trailing punctuation."
        )
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kiso-migrate-recipes-to-skills",
        description=(
            "Convert legacy ~/.kiso/recipes/*.md into ~/.kiso/skills/<name>/SKILL.md "
            "following the Kiso Skill Profile."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing skill files (default: skip)",
    )
    parser.add_argument(
        "--recipes-dir",
        type=Path,
        default=None,
        help="override source directory (default: ~/.kiso/recipes)",
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help="override target directory (default: ~/.kiso/skills)",
    )
    args = parser.parse_args(argv)

    summary = migrate_recipes(
        recipes_dir=args.recipes_dir,
        skills_dir=args.skills_dir,
        overwrite=args.overwrite,
    )

    if summary["source_missing"]:
        print(
            "No recipes directory found — nothing to migrate.",
            file=sys.stderr,
        )
        return 0

    migrated = summary["migrated"]
    skipped = summary["skipped_existing"]
    print(f"Migrated {len(migrated)} recipe(s) to skills:")
    for name in migrated:
        print(f"  + {name}")
    if skipped:
        print(f"\nSkipped {len(skipped)} (skill already exists):")
        for name in skipped:
            print(f"  = {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
