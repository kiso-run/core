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


def _discover_recipes(recipes_dir: Path) -> list[dict]:
    """Parse every ``*.md`` recipe under ``recipes_dir`` into a dict.

    Vendored inside the migration tool so we can drop the legacy
    ``kiso.recipe_loader`` module in the v0.10 cleanup. Mirrors the
    subset of the loader used by migration: frontmatter parsing,
    ``applies_to``/``excludes`` selector lists, and runtime-contract
    inference that feeds the synthetic ``## Worker`` section.
    """
    if not recipes_dir.is_dir():
        return []

    recipes: list[dict] = []
    for f in sorted(recipes_dir.glob("*.md")):
        parsed = _parse_recipe_file(f)
        if parsed:
            recipes.append(parsed)
    return recipes


def _parse_recipe_file(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None

    frontmatter = text[3:end].strip()
    body = text[end + 3:].strip()

    meta: dict[str, str] = {}
    for line in frontmatter.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon = line.find(":")
        if colon == -1:
            continue
        meta[line[:colon].strip()] = line[colon + 1:].strip()

    name = meta.get("name")
    summary = meta.get("summary")
    if not name or not summary:
        return None

    recipe: dict = {
        "name": name,
        "summary": summary,
        "instructions": body,
    }
    applies_to = _parse_selector_list(meta.get("applies_to", ""))
    excludes = _parse_selector_list(meta.get("excludes", ""))
    if applies_to:
        recipe["applies_to"] = applies_to
    if excludes:
        recipe["excludes"] = excludes
    contract = _infer_runtime_contract(recipe)
    if contract:
        recipe["runtime_contract"] = contract
    return recipe


def _parse_selector_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    items: list[str] = []
    for piece in raw.split(","):
        selector = piece.strip().strip("'\"").strip()
        if selector:
            items.append(selector)
    return items


def _infer_runtime_contract(recipe: dict) -> dict | None:
    text = " ".join(
        [str(recipe.get("summary") or ""), str(recipe.get("instructions") or "")]
    ).lower()
    if not text.strip():
        return None
    if any(token in text for token in ("valid json", "json object", "json format")):
        return {"output_format": "json_object"}
    if any(token in text for token in ("key-value", "key value", "key=value")):
        return {"output_format": "key_value"}
    return None


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
    recipes = _discover_recipes(recipes_root)

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
