"""M693-M694: CLI commands for preset management."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import shutil
import subprocess
import tempfile

from cli._http import cli_get
from cli.plugin_ops import fetch_registry, render_aligned_list


def _clone_and_load_preset(git_url: str):
    """Clone a preset repo to a temp dir, load and return the manifest."""
    from kiso.presets import load_preset

    tmpdir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", git_url, tmpdir + "/preset"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"error: git clone failed: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        preset_path = Path(tmpdir) / "preset" / "preset.toml"
        if not preset_path.is_file():
            print(f"error: preset.toml not found in cloned repo", file=sys.stderr)
            sys.exit(1)
        return load_preset(preset_path)
    except Exception as e:
        print(f"error: failed to load preset: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def preset_list(args: argparse.Namespace) -> None:
    """List presets from the official registry."""
    registry = fetch_registry()
    presets = registry.get("presets", [])
    if not presets:
        print("No presets available in registry.")
        return
    render_aligned_list(presets, "name", "description")


def preset_search(args: argparse.Namespace) -> None:
    """Search presets by name or description."""
    from kiso.registry import search_entries

    registry = fetch_registry()
    presets = registry.get("presets", [])
    results = search_entries(presets, args.query)
    if not results:
        print(f"No presets matching '{args.query}'.")
        return
    render_aligned_list(results, "name", "description")


def preset_install(args: argparse.Namespace) -> None:
    """Install a preset from a local path or registry name."""
    from cli.plugin_ops import require_admin
    require_admin()

    target = args.target
    path = Path(target)

    # Local path: direct file or directory with preset.toml
    if path.exists():
        if path.is_dir():
            path = path / "preset.toml"
        if not path.is_file():
            print(f"error: preset file not found: {path}", file=sys.stderr)
            sys.exit(1)
        from kiso.presets import load_preset
        manifest = load_preset(path)
    elif target.startswith("https://") or target.startswith("git@"):
        # Git URL: clone to temp, load preset.toml
        git_url = target
        manifest = _clone_and_load_preset(git_url)
    else:
        # Registry name: clone the official repo
        registry = fetch_registry()
        presets = registry.get("presets", [])
        match = next((p for p in presets if p["name"] == target), None)
        if not match:
            print(f"error: preset '{target}' not found in registry or as local path", file=sys.stderr)
            sys.exit(1)
        git_url = f"https://github.com/kiso-run/preset-{target}.git"
        manifest = _clone_and_load_preset(git_url)

    dry_run = getattr(args, "dry_run", False)

    from cli.preset_ops import install_preset
    install_preset(args, manifest, dry_run=dry_run)


def preset_show(args: argparse.Namespace) -> None:
    """Show preset contents (from local file or installed tracking)."""
    target = args.name
    path = Path(target)

    # Try as local path first
    if path.exists():
        if path.is_dir():
            path = path / "preset.toml"
        if path.is_file():
            from kiso.presets import load_preset
            manifest = load_preset(path)
            _render_manifest(manifest)
            return

    # Try installed preset
    from cli.preset_ops import _load_installed
    tracking = _load_installed(target)
    if tracking:
        print(f"Preset: {tracking['name']} v{tracking.get('version', '?')}")
        print(f"  Description: {tracking.get('description', '')}")
        if tracking.get("tools"):
            print(f"  Tools: {', '.join(tracking['tools'])}")
        if tracking.get("skills"):
            print(f"  Skills: {', '.join(tracking['skills'])}")
        if tracking.get("connectors"):
            print(f"  Connectors: {', '.join(tracking['connectors'])}")
        if tracking.get("fact_ids"):
            print(f"  Knowledge facts: {len(tracking['fact_ids'])} seeded")
        if tracking.get("behavior_ids"):
            print(f"  Behaviors: {len(tracking['behavior_ids'])} seeded")
        return

    # Try registry
    registry = fetch_registry()
    presets = registry.get("presets", [])
    match = next((p for p in presets if p["name"] == target), None)
    if match:
        print(f"Preset: {match['name']}")
        print(f"  Description: {match['description']}")
        print(f"  (Not installed — use 'kiso preset install' to install)")
        return

    print(f"error: preset '{target}' not found", file=sys.stderr)
    sys.exit(1)


def preset_installed(args: argparse.Namespace) -> None:
    """List installed presets."""
    from cli.preset_ops import list_installed_presets

    presets = list_installed_presets()
    if not presets:
        print("No presets installed.")
        return
    for p in presets:
        facts = len(p.get("fact_ids", []))
        behaviors = len(p.get("behavior_ids", []))
        version = p.get("version", "?")
        print(f"  {p['name']}  v{version}  — {p.get('description', '')}")
        print(f"    {facts} facts, {behaviors} behaviors")
        if p.get("tools"):
            print(f"    tools: {', '.join(p['tools'])}")
        if p.get("skills"):
            print(f"    skills: {', '.join(p['skills'])}")


def preset_remove(args: argparse.Namespace) -> None:
    """Remove an installed preset."""
    from cli.plugin_ops import require_admin
    require_admin()

    from cli.preset_ops import remove_preset
    remove_preset(args, args.name)


def _render_manifest(manifest) -> None:
    """Pretty-print a PresetManifest."""
    print(f"Preset: {manifest.name} v{manifest.version}")
    print(f"  Description: {manifest.description}")
    if manifest.tools:
        print(f"  Tools: {', '.join(manifest.tools)}")
    if manifest.skills:
        print(f"  Skills: {', '.join(manifest.skills)}")
    if manifest.connectors:
        print(f"  Connectors: {', '.join(manifest.connectors)}")
    if manifest.knowledge_facts:
        print(f"  Knowledge facts: {len(manifest.knowledge_facts)}")
        for f in manifest.knowledge_facts:
            content = f["content"]
            if len(content) > 70:
                content = content[:67] + "..."
            print(f"    - {content}")
    if manifest.behaviors:
        print(f"  Behaviors: {len(manifest.behaviors)}")
        for b in manifest.behaviors:
            print(f"    - {b}")
    if manifest.env_vars:
        print("  Environment variables:")
        for key, info in manifest.env_vars.items():
            req = "required" if info.get("required") else "optional"
            print(f"    {key} ({req}) — {info.get('description', '')}")
