"""``kiso init`` — bootstrap ``~/.kiso/config.toml`` from a bundled preset.

Writes the default config template (``kiso.config.CONFIG_TEMPLATE``)
followed by the ``[mcp.<name>]`` sections derived from the chosen
preset. Refuses to overwrite an existing file unless ``--force`` is
set.

After v0.10 lands, ``kiso init --preset default`` is the blessed
path for a new user. The auto-creation inside
:func:`kiso.config.load_config` remains for legacy install flows
but will be retired with the install-sh rewrite in M1533.
"""
from __future__ import annotations

import argparse
import sys

from kiso.config import CONFIG_PATH, CONFIG_TEMPLATE
from kiso.mcp_presets import (
    list_mcp_presets,
    load_mcp_preset,
    preset_mcp_servers,
    render_mcp_toml,
)
from kiso.trust_rules import (
    validate_preset_single_key,
    validate_preset_trust,
)


def run_init_command(args: argparse.Namespace) -> int:
    config_path = CONFIG_PATH
    preset_name = getattr(args, "preset", "default")
    force = getattr(args, "force", False)

    if config_path.exists() and not force:
        print(
            f"error: {config_path} already exists; rerun with --force to overwrite",
            file=sys.stderr,
        )
        return 1

    mcp_toml = _resolve_preset_toml(preset_name)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    full_contents = CONFIG_TEMPLATE
    if mcp_toml:
        full_contents = full_contents.rstrip() + "\n\n" + mcp_toml
    config_path.write_text(full_contents, encoding="utf-8")

    _print_post_init(config_path, preset_name)
    return 0


def _resolve_preset_toml(preset_name: str) -> str:
    if preset_name == "none":
        return ""

    available = list_mcp_presets()
    if preset_name not in available:
        print(
            f"error: preset {preset_name!r} not found; available: {available}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    preset = load_mcp_preset(preset_name)
    servers = preset_mcp_servers(preset)

    trust_violations = validate_preset_trust(servers)
    if trust_violations:
        _die_violations("trust rule", preset_name, trust_violations)
    key_violations = validate_preset_single_key(servers)
    if key_violations:
        _die_violations("single-key rule", preset_name, key_violations)

    return render_mcp_toml(servers)


def _die_violations(rule: str, preset_name: str, violations: list[str]) -> None:
    print(
        f"error: preset {preset_name!r} fails {rule}:", file=sys.stderr,
    )
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    raise SystemExit(1)


_BUNDLED_SKILLS_FOR_DEFAULT = (
    # (skill name, URL with pinned tag). Each is Tier 1 trusted in
    # SKILL_TIER1_PREFIXES, so the install runs silently (no per-
    # install consent prompt).
    (
        "message-attachment-receiver",
        "git+https://github.com/kiso-run/message-attachment-receiver-skill@v0.2.0",
    ),
)


def _print_post_init(config_path, preset_name: str) -> None:
    print(f"Config created at {config_path}")
    print()
    print(f"Applied preset: {preset_name}")
    print()
    print("Required environment:")
    print("  OPENROUTER_API_KEY   — mandatory for LLM calls and")
    print("                         any preset entry that uses it")
    print()
    print("Next steps:")
    print("  1. export OPENROUTER_API_KEY=sk-...")
    print("  2. kiso mcp test   # verify each preset server starts")
    if preset_name == "default" and _BUNDLED_SKILLS_FOR_DEFAULT:
        print("  3. install the bundled skills:")
        for _name, url in _BUNDLED_SKILLS_FOR_DEFAULT:
            print(f"       kiso skill install --from-url {url}")
