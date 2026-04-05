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
import re
import time
from pathlib import Path

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

_RECIPES_TTL = 30  # seconds

_cache: dict[Path, tuple[float, list[dict]]] = {}


def discover_recipes(recipes_dir: Path | None = None) -> list[dict]:
    """Discover .md recipe files and parse their frontmatter.

    Returns list of recipe dicts with required keys:
    {"name", "summary", "instructions", "path"}.

    Optional static applicability metadata is preserved when present:
    - applies_to: list[str]
    - excludes: list[str]

    Results are cached with a TTL to avoid repeated filesystem scans.
    """
    d = recipes_dir or (KISO_DIR / "recipes")
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

    Returns the parsed recipe dict or None on error.
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

    recipe = {
        "name": name,
        "summary": summary,
        "instructions": body,
        "path": str(path),
    }
    applies_to = _parse_selector_list(meta.get("applies_to", ""))
    excludes = _parse_selector_list(meta.get("excludes", ""))
    if applies_to:
        recipe["applies_to"] = applies_to
    if excludes:
        recipe["excludes"] = excludes
    runtime_contract = _infer_runtime_contract(recipe)
    if runtime_contract:
        recipe["runtime_contract"] = runtime_contract
    return recipe


def _parse_selector_list(raw: str) -> list[str]:
    """Parse a frontmatter selector list from comma-separated or [a, b] syntax."""
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    items = []
    for piece in raw.split(","):
        selector = piece.strip().strip("'\"").strip()
        if selector:
            items.append(selector)
    return items


def filter_recipes_for_message(recipes: list[dict], message: str) -> list[dict]:
    """Apply optional static applicability metadata to a recipe list.

    Recipes without metadata remain eligible by default.
    - applies_to: at least one selector must match the user request
    - excludes: any selector match excludes the recipe
    """
    if not recipes or not message:
        return recipes

    filtered: list[dict] = []
    for recipe in recipes:
        applies_to = recipe.get("applies_to") or []
        excludes = recipe.get("excludes") or []
        if applies_to and not any(_selector_matches_message(message, s) for s in applies_to):
            continue
        if any(_selector_matches_message(message, s) for s in excludes):
            continue
        filtered.append(recipe)
    return filtered


def _selector_matches_message(message: str, selector: str) -> bool:
    """Match selector against a message with word-aware single-token handling."""
    selector = selector.strip().lower()
    if not selector:
        return False
    message_norm = " ".join(message.lower().split())
    selector_norm = " ".join(selector.split())
    if not selector_norm:
        return False
    if " " in selector_norm:
        return selector_norm in message_norm
    return re.search(rf"\b{re.escape(selector_norm)}\b", message_norm) is not None


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


def build_recipe_runtime_contracts_text(recipes: list[dict]) -> str:
    """Format runtime-relevant recipe contracts for non-planner consumers."""
    if not recipes:
        return ""
    lines: list[str] = []
    for recipe in recipes:
        contract = recipe.get("runtime_contract") or {}
        output_format = contract.get("output_format")
        if output_format == "json_object":
            lines.append(
                f"- {recipe['name']}: when producing structured output, prefer a valid JSON object"
            )
        elif output_format == "key_value":
            lines.append(
                f"- {recipe['name']}: when producing structured output, prefer key=value lines"
            )
    return "\n".join(lines)


def _infer_runtime_contract(recipe: dict) -> dict | None:
    """Infer lightweight runtime hints from recipe instructions.

    This keeps recipes general-purpose while allowing execution/review layers to
    carry a small amount of structured intent beyond planner prose.
    """
    text = " ".join([
        str(recipe.get("summary") or ""),
        str(recipe.get("instructions") or ""),
    ]).lower()
    if not text.strip():
        return None

    if any(token in text for token in ("valid json", "json object", "json format")):
        return {"output_format": "json_object"}
    if any(token in text for token in ("key-value", "key value", "key=value")):
        return {"output_format": "key_value"}
    return None
