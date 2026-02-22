"""Skill management CLI commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.plugin_ops import (
    OFFICIAL_ORG,
    _GIT_ENV,
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

    target = args.target
    if _is_url(target):
        git_url = target
        name = args.name or url_to_name(target)
        is_official = False
    else:
        git_url = f"https://github.com/{OFFICIAL_ORG}/{OFFICIAL_PREFIX}{target}.git"
        name = target
        is_official = True

    # --show-deps: clone to temp, show deps.sh, cleanup
    if args.show_deps:
        tmpdir = tempfile.mkdtemp()
        try:
            result = subprocess.run(
                ["git", "clone", git_url, tmpdir],
                capture_output=True, text=True, env=_GIT_ENV,
            )
            if result.returncode != 0:
                if is_official and _is_repo_not_found(result.stderr):
                    print(f"error: skill '{name}' not found in {OFFICIAL_ORG} org")
                else:
                    print(f"error: git clone failed: {result.stderr.strip()}")
                sys.exit(1)
            deps_path = Path(tmpdir) / "deps.sh"
            if deps_path.exists():
                print(deps_path.read_text())
            else:
                print("No deps.sh in this skill.")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return

    skill_dir = SKILLS_DIR / name

    # Check if already installed
    if skill_dir.exists():
        print(f"error: skill '{name}' is already installed at {skill_dir}")
        sys.exit(1)

    try:
        # Ensure parent dir exists, then clone (creates skill_dir)
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["git", "clone", git_url, str(skill_dir)],
            capture_output=True, text=True, env=_GIT_ENV,
        )
        if result.returncode != 0:
            if is_official and _is_repo_not_found(result.stderr):
                print(f"error: skill '{name}' not found in {OFFICIAL_ORG} org")
            else:
                print(f"error: git clone failed: {result.stderr.strip()}")
            raise RuntimeError("git clone failed")

        # Mark as installing (after clone succeeds)
        (skill_dir / ".installing").touch()

        # Validate manifest
        toml_path = skill_dir / "kiso.toml"
        if not toml_path.exists():
            print("error: kiso.toml not found in cloned repo")
            raise RuntimeError("missing kiso.toml")

        import tomllib

        with open(toml_path, "rb") as f:
            manifest = tomllib.load(f)

        errors = _validate_manifest(manifest, skill_dir)
        if errors:
            for e in errors:
                print(f"error: {e}")
            raise RuntimeError("manifest validation failed")

        # Unofficial repo warning
        if not is_official:
            print("WARNING: This is an unofficial skill repo.")
            deps_path = skill_dir / "deps.sh"
            if deps_path.exists():
                print("\ndeps.sh contents:")
                print(deps_path.read_text())
            answer = input("Continue? [y/N] ").strip().lower()
            if answer != "y":
                print("Installation cancelled.")
                raise RuntimeError("cancelled")

        # Run deps.sh if present and not --no-deps
        deps_path = skill_dir / "deps.sh"
        if deps_path.exists() and not args.no_deps:
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: deps.sh failed: {result.stderr.strip()}")

        # uv sync
        subprocess.run(
            ["uv", "sync"],
            cwd=str(skill_dir),
            capture_output=True, text=True,
        )

        # Check binary deps
        skill_info = {"path": str(skill_dir)}
        missing = check_deps(skill_info)
        if missing:
            print(f"warning: missing binaries: {', '.join(missing)}")

        # Check env vars
        kiso_section = manifest.get("kiso", {})
        skill_section = kiso_section.get("skill", {})
        env_decl = skill_section.get("env", {})
        skill_name = kiso_section.get("name", name)
        for key in env_decl:
            var_name = _env_var_name(skill_name, key)
            if not os.environ.get(var_name):
                print(f"warning: {var_name} not set")

        # Create usage_guide.local.md from toml default
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

        # Remove installing marker
        installing = skill_dir / ".installing"
        if installing.exists():
            installing.unlink()

        print(f"Skill '{name}' installed successfully.")
        from kiso.sysenv import invalidate_cache
        invalidate_cache()

    except Exception:
        # Cleanup on failure
        if skill_dir.exists():
            shutil.rmtree(skill_dir, ignore_errors=True)
        sys.exit(1)


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

        # deps.sh
        deps_path = skill_dir / "deps.sh"
        if deps_path.exists():
            result = subprocess.run(
                ["bash", str(deps_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"warning: deps.sh failed for '{name}': {result.stderr.strip()}")

        # uv sync
        subprocess.run(
            ["uv", "sync"],
            cwd=str(skill_dir),
            capture_output=True, text=True,
        )

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
