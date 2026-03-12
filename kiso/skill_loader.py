"""Discover and parse .md skill files (lightweight planner instructions).

A skill is a markdown file in ~/.kiso/skills/ with YAML frontmatter:

    ---
    name: data-analyst
    summary: Guides planner for data analysis tasks
    ---

    (planner instructions body)

Skills are additive context for the planner — they don't execute anything.
The briefer decides which skills are relevant for each request.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

_SKILLS_DIR = KISO_DIR / "skills"
_SKILLS_TTL = 30  # seconds

_cache: dict[Path, tuple[float, list[dict]]] = {}


def discover_md_skills(skills_dir: Path | None = None) -> list[dict]:
    """Discover .md skill files and parse their frontmatter.

    Returns list of {"name", "summary", "instructions", "path"} dicts.
    Results are cached with a TTL to avoid repeated filesystem scans.
    """
    d = skills_dir or _SKILLS_DIR
    now = time.monotonic()

    cached = _cache.get(d)
    if cached and (now - cached[0]) < _SKILLS_TTL:
        return cached[1]

    if not d.is_dir():
        _cache[d] = (now, [])
        return []

    skills: list[dict] = []
    for f in sorted(d.glob("*.md")):
        parsed = _parse_skill_file(f)
        if parsed:
            skills.append(parsed)

    _cache[d] = (now, skills)
    log.debug("Discovered %d MD skills in %s", len(skills), d)
    return skills


def invalidate_md_skills_cache() -> None:
    """Clear the skills cache (used after install/remove)."""
    _cache.clear()


def _parse_skill_file(path: Path) -> dict | None:
    """Parse a .md skill file with YAML frontmatter.

    Returns {"name", "summary", "instructions", "path"} or None on error.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("Cannot read skill file: %s", path)
        return None

    # Parse frontmatter between --- markers
    if not text.startswith("---"):
        log.warning("Skill file missing frontmatter: %s", path)
        return None

    end = text.find("---", 3)
    if end == -1:
        log.warning("Skill file has unclosed frontmatter: %s", path)
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
        log.warning("Skill file missing 'name' in frontmatter: %s", path)
        return None
    if not summary:
        log.warning("Skill file missing 'summary' in frontmatter: %s", path)
        return None

    return {
        "name": name,
        "summary": summary,
        "instructions": body,
        "path": str(path),
    }


def build_planner_skill_list(skills: list[dict]) -> str:
    """Format skill list for planner context.

    Each entry: - {name} — {summary} + instructions block.
    """
    if not skills:
        return ""
    parts: list[str] = []
    for s in skills:
        entry = f"- {s['name']} — {s['summary']}"
        if s.get("instructions"):
            # Indent instructions under the skill entry
            indented = "\n".join(
                f"  {line}" for line in s["instructions"].splitlines()
            )
            entry += f"\n{indented}"
        parts.append(entry)
    return "\n".join(parts)
