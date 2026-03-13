"""MD-based skill management CLI commands.

Skills are lightweight .md files with YAML frontmatter that provide
planner instructions. They live in ~/.kiso/skills/.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.skill_loader import discover_md_skills, invalidate_md_skills_cache

SKILLS_DIR = KISO_DIR / "skills"


def run_skill_command(args) -> None:
    """Dispatch to the appropriate skill subcommand."""
    from cli.plugin_ops import dispatch_subcommand
    dispatch_subcommand(args, "skill_command", {
        "list": lambda _: _skill_list(),
        "install": _skill_install, "remove": _skill_remove,
    }, "usage: kiso skill {list,install,remove}")


def _skill_list() -> None:
    """List installed MD skills."""
    invalidate_md_skills_cache()
    skills = discover_md_skills(SKILLS_DIR)
    if not skills:
        print("No skills installed.")
        return
    max_name = max(len(s["name"]) for s in skills)
    for s in skills:
        print(f"  {s['name'].ljust(max_name)}  — {s['summary']}")


def _skill_install(args) -> None:
    """Install an MD skill from a local file path."""
    source = Path(args.source)
    if not source.exists():
        print(f"error: file not found: {source}", file=sys.stderr)
        sys.exit(1)
    if not source.suffix == ".md":
        print("error: skill file must be a .md file", file=sys.stderr)
        sys.exit(1)

    # Validate the file has proper frontmatter
    from kiso.skill_loader import _parse_skill_file
    parsed = _parse_skill_file(source)
    if parsed is None:
        print("error: invalid skill file — must have YAML frontmatter with 'name' and 'summary'",
              file=sys.stderr)
        sys.exit(1)

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = SKILLS_DIR / source.name
    if dest.exists():
        print(f"Skill '{source.name}' already installed — updating.")
    shutil.copy2(source, dest)
    invalidate_md_skills_cache()
    print(f"Skill '{parsed['name']}' installed.")


def _skill_remove(args) -> None:
    """Remove an installed MD skill."""
    name = args.name
    # Try exact filename first, then match by name
    target = SKILLS_DIR / f"{name}.md"
    if not target.exists():
        target = SKILLS_DIR / name
    if not target.exists():
        # Search by skill name in frontmatter
        invalidate_md_skills_cache()
        skills = discover_md_skills(SKILLS_DIR)
        for s in skills:
            if s["name"] == name:
                target = Path(s["path"])
                break
    if not target.exists():
        print(f"error: skill '{name}' is not installed", file=sys.stderr)
        sys.exit(1)

    target.unlink()
    invalidate_md_skills_cache()
    print(f"Skill '{name}' removed.")
