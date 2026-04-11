"""M692: Preset install/remove orchestration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.presets import PresetManifest
from cli.render import die

PRESETS_DIR = KISO_DIR / "presets"


def _installed_path(name: str) -> Path:
    """Return the path to the installed.json tracking file for a preset."""
    return PRESETS_DIR / f"{name}.installed.json"


def _load_installed(name: str) -> dict | None:
    """Load the installed.json tracking file for a preset, or None."""
    path = _installed_path(name)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_installed(name: str, data: dict) -> None:
    """Save the installed.json tracking file for a preset."""
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    path = _installed_path(name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_installed_presets() -> list[dict]:
    """Return list of installed presets from tracking files."""
    if not PRESETS_DIR.is_dir():
        return []
    results = []
    for f in sorted(PRESETS_DIR.iterdir()):
        if f.suffix == ".json" and f.stem.endswith(".installed"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                results.append(data)
            except (json.JSONDecodeError, OSError):
                continue
    return results


from cli.render import _GREEN, _RED, _RESET, detect_caps


def _c(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}" if detect_caps().color else text


def _auto_install_plugins(names: list[str], install_fn) -> list[str]:
    """Auto-install plugins by name. Returns list of successfully installed names."""
    import argparse
    import contextlib
    import io

    total = len(names)
    installed: list[str] = []
    for i, name in enumerate(names, 1):
        fake_args = argparse.Namespace(
            target=name, name=None, show_deps=False, no_deps=False,
        )
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                install_fn(fake_args)
            installed.append(name)
            print(f"  [{i}/{total}] {name} {_c('✓', _GREEN)}")
        except (SystemExit, Exception):
            print(f"  [{i}/{total}] {name} {_c('✗', _RED)}")
    return installed


def install_preset(args, manifest: PresetManifest, *, dry_run: bool = False) -> None:
    """Orchestrate preset installation.

    1. Seed knowledge facts via POST /knowledge
    2. Seed behaviors via POST /knowledge (category=behavior)
    3. Print tool/skill install instructions
    4. Save tracking file
    """
    from cli._http import cli_post

    # Check if already installed
    existing = _load_installed(manifest.name)
    if existing:
        print(f"Preset '{manifest.name}' is already installed.")
        print("Use 'kiso preset remove' first to reinstall.")
        return

    if dry_run:
        print(f"Dry run — preset '{manifest.name}' v{manifest.version}")
        print(f"  Description: {manifest.description}")
        if manifest.wrappers:
            print(f"  Wrappers to install: {', '.join(manifest.wrappers)}")
        if manifest.skills:
            print(f"  Skills to install: {', '.join(manifest.skills)}")
        if manifest.connectors:
            print(f"  Connectors to install: {', '.join(manifest.connectors)}")
        if manifest.knowledge_facts:
            print(f"  Knowledge facts to seed: {len(manifest.knowledge_facts)}")
        if manifest.behaviors:
            print(f"  Behaviors to seed: {len(manifest.behaviors)}")
            for b in manifest.behaviors:
                print(f"    - {b}")
        if manifest.recipes:
            print(f"  Recipes to install: {len(manifest.recipes)}")
            for r in manifest.recipes:
                print(f"    - {r['name']}: {r['summary']}")
        if manifest.env_vars:
            print(f"  Env vars: {', '.join(manifest.env_vars.keys())}")
        return

    fact_ids: list[int] = []
    behavior_ids: list[int] = []

    # Seed knowledge facts
    for fact in manifest.knowledge_facts:
        body: dict = {
            "content": fact["content"],
            "category": fact.get("category", "general"),
        }
        tags = fact.get("tags")
        if tags:
            body["tags"] = tags
        try:
            resp = cli_post(args, "/knowledge", json_body=body)
            data = resp.json()
            fact_ids.append(data["id"])
        except SystemExit:
            print(f"warning: failed to seed fact: {fact['content'][:60]}", file=sys.stderr)

    # Seed behaviors
    for behavior in manifest.behaviors:
        body = {"content": behavior, "category": "behavior"}
        try:
            resp = cli_post(args, "/knowledge", json_body=body)
            data = resp.json()
            behavior_ids.append(data["id"])
        except SystemExit:
            print(f"warning: failed to seed behavior: {behavior[:60]}", file=sys.stderr)

    # auto-install tools and connectors
    installed_wrappers: list[str] = []
    installed_connectors: list[str] = []

    if manifest.wrappers:
        from cli.wrapper import _wrapper_install
        installed_wrappers = _auto_install_plugins(manifest.wrappers, _wrapper_install)

    if manifest.connectors:
        from cli.connector import _connector_install
        installed_connectors = _auto_install_plugins(manifest.connectors, _connector_install)

    # Install recipes
    recipe_files: list[str] = []
    if manifest.recipes:
        recipes_dir = KISO_DIR / "recipes"
        recipes_dir.mkdir(parents=True, exist_ok=True)
        for recipe in manifest.recipes:
            filename = f"{recipe['name']}.md"
            recipe_path = recipes_dir / filename
            content = (
                f"---\nname: {recipe['name']}\n"
                f"summary: {recipe['summary']}\n---\n"
                f"{recipe['body']}\n"
            )
            recipe_path.write_text(content, encoding="utf-8")
            recipe_files.append(filename)

    # Save tracking file
    tracking = {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "fact_ids": fact_ids,
        "behavior_ids": behavior_ids,
        "wrappers": manifest.wrappers,
        "skills": manifest.skills,
        "connectors": manifest.connectors,
        "installed_wrappers": installed_wrappers,
        "installed_connectors": installed_connectors,
        "recipe_files": recipe_files,
    }
    _save_installed(manifest.name, tracking)

    # Report — clean summary
    parts: list[str] = []
    if installed_wrappers:
        parts.append(f"{len(installed_wrappers)} tools")
    skipped_tools = set(manifest.wrappers) - set(installed_wrappers)
    if skipped_tools:
        parts.append(f"{len(skipped_tools)} tools skipped")
    if installed_connectors:
        parts.append(f"{len(installed_connectors)} connectors")
    if behavior_ids:
        parts.append(f"{len(behavior_ids)} behaviors")
    if fact_ids:
        parts.append(f"{len(fact_ids)} facts")
    if recipe_files:
        parts.append(f"{len(recipe_files)} recipes")
    summary = ", ".join(parts) if parts else "no components"
    print(f"\n  {_c('✓', _GREEN)} Preset installed — {summary}")
    if skipped_tools:
        print(f"  Skipped (install manually): {', '.join(skipped_tools)}")

    # Env var hints
    if manifest.env_vars:
        print("\n  Environment variables:")
        for key, info in manifest.env_vars.items():
            req = "required" if info.get("required") else "optional"
            desc = info.get("description", "")
            print(f"    {key} ({req}) — {desc}")


def remove_preset(args, name: str) -> None:
    """Remove a preset: delete tracked facts/behaviors, remove tracking file."""
    from cli._http import cli_delete

    tracking = _load_installed(name)
    if not tracking:
        die(f"preset '{name}' is not installed")

    removed_facts = 0
    removed_behaviors = 0

    # Remove facts
    for fid in tracking.get("fact_ids", []):
        try:
            cli_delete(args, f"/knowledge/{fid}")
            removed_facts += 1
        except SystemExit:
            pass  # fact may have been manually deleted

    # Remove behaviors
    for bid in tracking.get("behavior_ids", []):
        try:
            cli_delete(args, f"/knowledge/{bid}")
            removed_behaviors += 1
        except SystemExit:
            pass

    # Remove recipe files
    removed_recipes = 0
    recipes_dir = KISO_DIR / "recipes"
    for filename in tracking.get("recipe_files", []):
        recipe_path = recipes_dir / filename
        if recipe_path.is_file():
            recipe_path.unlink()
            removed_recipes += 1

    # Remove tracking file
    path = _installed_path(name)
    if path.exists():
        path.unlink()

    print(f"Preset '{name}' removed.")
    if removed_facts:
        print(f"  Removed {removed_facts} knowledge facts.")
    if removed_behaviors:
        print(f"  Removed {removed_behaviors} behaviors.")
    if removed_recipes:
        print(f"  Removed {removed_recipes} recipes.")
