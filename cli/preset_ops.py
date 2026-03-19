"""M692: Preset install/remove orchestration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.presets import PresetManifest

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


def _auto_install_tools(tool_names: list[str]) -> list[str]:
    """Auto-install tools by name. Returns list of successfully installed names."""
    import argparse
    import io
    import contextlib
    from cli.tool import _tool_install

    total = len(tool_names)
    installed: list[str] = []
    for i, name in enumerate(tool_names, 1):
        fake_args = argparse.Namespace(
            target=name, name=None, show_deps=False, no_deps=False,
        )
        try:
            # Suppress individual tool install output (stdout + stderr)
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                _tool_install(fake_args)
            installed.append(name)
            print(f"  [{i}/{total}] {name} {_c('✓', _GREEN)}")
        except (SystemExit, Exception):
            print(f"  [{i}/{total}] {name} {_c('✗', _RED)}")
    return installed


def _auto_install_connectors(connector_names: list[str]) -> list[str]:
    """Auto-install connectors by name. Returns list of successfully installed names."""
    import argparse
    import contextlib
    import io
    from cli.connector import _connector_install

    total = len(connector_names)
    installed: list[str] = []
    for i, name in enumerate(connector_names, 1):
        fake_args = argparse.Namespace(
            target=name, name=None, show_deps=False, no_deps=False,
        )
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                _connector_install(fake_args)
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
        if manifest.tools:
            print(f"  Tools to install: {', '.join(manifest.tools)}")
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
    installed_tools: list[str] = []
    installed_connectors: list[str] = []

    if manifest.tools:
        installed_tools = _auto_install_tools(manifest.tools)

    if manifest.connectors:
        installed_connectors = _auto_install_connectors(manifest.connectors)

    # Save tracking file
    tracking = {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "fact_ids": fact_ids,
        "behavior_ids": behavior_ids,
        "tools": manifest.tools,
        "skills": manifest.skills,
        "connectors": manifest.connectors,
        "installed_tools": installed_tools,
        "installed_connectors": installed_connectors,
    }
    _save_installed(manifest.name, tracking)

    # Report — clean summary
    parts: list[str] = []
    if installed_tools:
        parts.append(f"{len(installed_tools)} tools")
    skipped_tools = set(manifest.tools) - set(installed_tools)
    if skipped_tools:
        parts.append(f"{len(skipped_tools)} tools skipped")
    if installed_connectors:
        parts.append(f"{len(installed_connectors)} connectors")
    if behavior_ids:
        parts.append(f"{len(behavior_ids)} behaviors")
    if fact_ids:
        parts.append(f"{len(fact_ids)} facts")
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
        print(f"error: preset '{name}' is not installed", file=sys.stderr)
        sys.exit(1)

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

    # Remove tracking file
    path = _installed_path(name)
    if path.exists():
        path.unlink()

    print(f"Preset '{name}' removed.")
    if removed_facts:
        print(f"  Removed {removed_facts} knowledge facts.")
    if removed_behaviors:
        print(f"  Removed {removed_behaviors} behaviors.")
