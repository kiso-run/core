""": CLI commands for persona-preset management.

A "preset" here is a ``preset.toml`` manifest with knowledge facts,
behaviors, and env-var hints — a bundled persona a user can drop
into their instance. Not to be confused with ``kiso init
--preset``, which seeds ``mcp.json`` from a bundled MCP server
preset under ``kiso/presets/``.

v0.10 retired the github-hosted ``registry.json`` index; installs
are now path- or URL-based. No more name lookup.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import shutil
import subprocess
import tempfile

from cli._admin import require_admin
from cli._http import cli_get
from cli.render import die


def _clone_and_load_preset(git_url: str):
    """Clone a preset repo to a temp dir, load and return the manifest."""
    from kiso.presets import load_preset

    print("  Fetching preset...")
    tmpdir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", git_url, tmpdir + "/preset"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            die(f"git clone failed: {result.stderr.strip()}")
        preset_path = Path(tmpdir) / "preset" / "preset.toml"
        if not preset_path.is_file():
            die("preset.toml not found in cloned repo")
        return load_preset(preset_path)
    except Exception as e:
        die(f"failed to load preset: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _show_preset_summary(manifest) -> None:
    """Display preset contents summary before installing."""
    header = f"Installing preset '{manifest.name}' v{manifest.version}"
    print(f"  {header}")
    print(f"  {'─' * len(header)}")
    if manifest.wrappers:
        print(f"  Wrappers:  {', '.join(manifest.wrappers)}")
    if manifest.connectors:
        print(f"  Connectors: {', '.join(manifest.connectors)}")
    if manifest.behaviors:
        print(f"  Behaviors: {len(manifest.behaviors)} guidelines")
    if manifest.recipes:
        print(f"  Recipes:   {len(manifest.recipes)}")
    print()


def preset_install(args: argparse.Namespace) -> None:
    """Install a persona preset from a local path or git URL.

    Registry-name lookup is retired in v0.10 — pass a concrete
    path or URL. For the bundled MCP default preset, use
    ``kiso init --preset default`` instead.
    """
    require_admin()

    target = args.target
    path = Path(target)

    # Local path: direct file or directory with preset.toml
    if path.exists():
        if path.is_dir():
            path = path / "preset.toml"
        if not path.is_file():
            die(f"preset file not found: {path}")
        from kiso.presets import load_preset
        manifest = load_preset(path)
    elif target.startswith(("https://", "git@", "git+")):
        # Git URL: clone to temp, load preset.toml
        manifest = _clone_and_load_preset(target)
    else:
        die(
            f"preset '{target}' not found as a local path and is not a "
            f"git URL.\n"
            f"  v0.10 retired the registry name lookup — pass a path "
            f"or a `https://` / `git@` URL.\n"
            f"  For the bundled MCP default preset, run: "
            f"`kiso init --preset default`"
        )

    # Show preset contents before installing
    _show_preset_summary(manifest)

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
        if tracking.get("wrappers"):
            print(f"  Wrappers: {', '.join(tracking['wrappers'])}")
        if tracking.get("connectors"):
            print(f"  Connectors: {', '.join(tracking['connectors'])}")
        if tracking.get("fact_ids"):
            print(f"  Knowledge facts: {len(tracking['fact_ids'])} seeded")
        if tracking.get("behavior_ids"):
            print(f"  Behaviors: {len(tracking['behavior_ids'])} seeded")
        return

    die(
        f"preset '{target}' not found as a local path, git URL, or "
        f"already-installed preset. Registry-name lookup is retired."
    )


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
        if p.get("wrappers"):
            print(f"    wrappers: {', '.join(p['wrappers'])}")


def preset_remove(args: argparse.Namespace) -> None:
    """Remove an installed preset."""
    require_admin()

    from cli.preset_ops import remove_preset
    remove_preset(args, args.name)


def _render_manifest(manifest) -> None:
    """Pretty-print a PresetManifest."""
    print(f"Preset: {manifest.name} v{manifest.version}")
    print(f"  Description: {manifest.description}")
    if manifest.wrappers:
        print(f"  Wrappers: {', '.join(manifest.wrappers)}")
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
