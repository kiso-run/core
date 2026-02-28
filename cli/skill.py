"""Skill management CLI commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from kiso.config import KISO_DIR
from cli.plugin_ops import (
    OFFICIAL_ORG,
    _GIT_ENV,
    _plugin_install,
    fetch_registry,
    is_repo_not_found,
    is_url,
    require_admin,
    search_entries,
    url_to_name,
)
from kiso.skills import _env_var_name, _validate_manifest, check_deps, discover_skills

SKILLS_DIR = KISO_DIR / "skills"
OFFICIAL_PREFIX = "skill-"

# Backward-compatible aliases for cli_connector and tests that import from here
_is_url = is_url
_is_repo_not_found = is_repo_not_found
_require_admin = require_admin
_fetch_registry = fetch_registry
_search_entries = search_entries


def _skill_post_install(manifest: dict, skill_dir: Path, name: str) -> None:
    """Skill-specific post-install steps: env var warnings, usage guide, git exclude."""
    kiso_section = manifest.get("kiso", {})
    skill_section = kiso_section.get("skill", {})
    env_decl = skill_section.get("env", {})
    skill_name = kiso_section.get("name", name)
    for key in env_decl:
        var_name = _env_var_name(skill_name, key)
        if not os.environ.get(var_name):
            print(f"warning: {var_name} not set")

    # Create usage_guide.local.md from toml default if not already present
    usage_guide = skill_section.get("usage_guide", "")
    override_path = skill_dir / "usage_guide.local.md"
    if usage_guide and not override_path.exists():
        override_path.write_text(usage_guide + "\n")

    # Add usage_guide.local.md to .git/info/exclude so git pull won't conflict
    exclude_path = skill_dir / ".git" / "info" / "exclude"
    if exclude_path.exists():
        exclude_content = exclude_path.read_text()
        if "usage_guide.local.md" not in exclude_content:
            with open(exclude_path, "a") as f:
                f.write("\nusage_guide.local.md\n")


def run_skill_command(args) -> None:
    """Dispatch to the appropriate skill subcommand."""
    cmd = getattr(args, "skill_command", None)
    if cmd is None:
        print("usage: kiso skill {list,search,install,update,remove}")
        sys.exit(1)
    elif cmd == "list":
        _skill_list(args)
    elif cmd == "search":
        _skill_search(args)
    elif cmd == "install":
        _skill_install(args)
    elif cmd == "update":
        _skill_update(args)
    elif cmd == "remove":
        _skill_remove(args)


def _skill_list(args) -> None:
    """List installed skills."""
    skills = discover_skills()
    if not skills:
        print("No skills installed.")
        return

    # Column-align by max name/version width
    max_name = max(len(s["name"]) for s in skills)
    max_ver = max(len(s["version"]) for s in skills)
    for s in skills:
        name = s["name"].ljust(max_name)
        ver = s["version"].ljust(max_ver)
        summary = s.get("summary", s.get("description", ""))
        print(f"  {name}  {ver}  — {summary}")



def _skill_search(args) -> None:
    """Search official skills from the registry."""
    registry = _fetch_registry()
    results = _search_entries(registry.get("skills", []), args.query)

    if not results:
        print("No skills found.")
        return

    max_name = max(len(r["name"]) for r in results)
    for r in results:
        print(f"  {r['name'].ljust(max_name)}  — {r.get('description', '')}")


def _skill_install(args) -> None:
    """Install a skill from official repo or git URL."""
    _require_admin()
    _plugin_install(
        plugin_type="skill",
        official_prefix=OFFICIAL_PREFIX,
        parent_dir=SKILLS_DIR,
        validate_fn=_validate_manifest,
        check_deps_fn=check_deps,
        args=args,
        post_install=_skill_post_install,
    )


def _skill_update(args) -> None:
    """Update an installed skill or all skills."""
    _require_admin()

    target = args.target
    if target == "all":
        if not SKILLS_DIR.is_dir():
            print("No skills installed.")
            return
        names = [d.name for d in sorted(SKILLS_DIR.iterdir()) if d.is_dir()]
        if not names:
            print("No skills installed.")
            return
    else:
        names = [target]

    for name in names:
        skill_dir = SKILLS_DIR / name
        if not skill_dir.exists():
            print(f"error: skill '{name}' is not installed")
            sys.exit(1)

        # git pull
        result = subprocess.run(
            ["git", "pull"],
            cwd=str(skill_dir),
            capture_output=True, text=True, env=_GIT_ENV,
        )
        if result.returncode != 0:
            print(f"error: git pull failed for '{name}': {result.stderr.strip()}")
            sys.exit(1)

        # uv sync first (deps.sh may need packages installed by uv)
        subprocess.run(
            ["uv", "sync"],
            cwd=str(skill_dir),
            capture_output=True, text=True,
        )

        # deps.sh
        deps_path = skill_dir / "deps.sh"
        if deps_path.exists():
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: deps.sh failed for '{name}': {result.stderr.strip()}")

        # check deps
        skill_info = {"path": str(skill_dir)}
        missing = check_deps(skill_info)
        if missing:
            print(f"warning: '{name}' missing binaries: {', '.join(missing)}")

        print(f"Skill '{name}' updated.")
        from kiso.sysenv import invalidate_cache
        invalidate_cache()


def _skill_remove(args) -> None:
    """Remove an installed skill."""
    _require_admin()

    name = args.name
    skill_dir = SKILLS_DIR / name
    if not skill_dir.exists():
        print(f"error: skill '{name}' is not installed")
        sys.exit(1)

    shutil.rmtree(skill_dir)
    print(f"Skill '{name}' removed.")
    from kiso.sysenv import invalidate_cache
    invalidate_cache()
