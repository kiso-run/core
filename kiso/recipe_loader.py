"""Discover and parse .md recipe files (lightweight planner instructions).

A recipe is a markdown file in ~/.kiso/recipes/ with YAML frontmatter:

    ---
    name: data-analyst
    summary: Guides planner for data analysis tasks
    ---

    (planner instructions body)

Recipes are additive context for the planner — they don't execute anything.
The briefer decides which recipes are relevant for each request.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

_RECIPES_DIR = KISO_DIR / "recipes"
_RECIPES_TTL = 30  # seconds

_cache: dict[Path, tuple[float, list[dict]]] = {}


def discover_recipes(recipes_dir: Path | None = None) -> list[dict]:
    """Discover .md recipe files and parse their frontmatter.

    Returns list of {"name", "summary", "instructions", "path"} dicts.
    Results are cached with a TTL to avoid repeated filesystem scans.
    """
    d = recipes_dir or _RECIPES_DIR
    now = time.monotonic()

    cached = _cache.get(d)
    if cached and (now - cached[0]) < _RECIPES_TTL:
        return cached[1]

    if not d.is_dir():
        _cache[d] = (now, [])
        return []

    recipes: list[dict] = []
    for f in sorted(d.glob("*.md")):
        parsed = _parse_recipe_file(f)
        if parsed:
            recipes.append(parsed)

    _cache[d] = (now, recipes)
    log.debug("Discovered %d recipes in %s", len(recipes), d)
    return recipes


def invalidate_recipes_cache() -> None:
    """Clear the recipes cache (used after install/remove)."""
    _cache.clear()


def _parse_recipe_file(path: Path) -> dict | None:
    """Parse a .md recipe file with YAML frontmatter.

    Returns {"name", "summary", "instructions", "path"} or None on error.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("Cannot read recipe file: %s", path)
        return None

    # Parse frontmatter between --- markers
    if not text.startswith("---"):
        log.warning("Recipe file missing frontmatter: %s", path)
        return None

    end = text.find("---", 3)
    if end == -1:
        log.warning("Recipe file has unclosed frontmatter: %s", path)
        return None

    frontmatter = text[3:end].strip()
    body = text[end + 3:].strip()

    # Simple YAML parsing (key: value) — no external dependency needed
    meta: dict[str, str] = {}
    for line in frontmatter.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon = line.find(":")
        if colon == -1:
            continue
        key = line[:colon].strip()
        val = line[colon + 1:].strip()
        meta[key] = val

    name = meta.get("name")
    summary = meta.get("summary")
    if not name:
        log.warning("Recipe file missing 'name' in frontmatter: %s", path)
        return None
    if not summary:
        log.warning("Recipe file missing 'summary' in frontmatter: %s", path)
        return None

    return {
        "name": name,
        "summary": summary,
        "instructions": body,
        "path": str(path),
    }


def build_planner_recipe_list(recipes: list[dict]) -> str:
    """Format recipe list for planner context.

    Each entry: - {name} — {summary} + instructions block.
    """
    if not recipes:
        return ""
    parts: list[str] = []
    for r in recipes:
        entry = f"- {r['name']} — {r['summary']}"
        if r.get("instructions"):
            # Indent instructions under the recipe entry
            indented = "\n".join(
                f"  {line}" for line in r["instructions"].splitlines()
            )
            entry += f"\n{indented}"
        parts.append(entry)
    return "\n".join(parts)
