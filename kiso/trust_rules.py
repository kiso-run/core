"""Trust and single-key rules for the shipped default MCP preset.

These rules are enforced as CI-level tests against
``kiso/presets/default.mcp.json``. They exist because the default
preset is distributed with kiso and any entry that slips in has
direct blast radius on every fresh install — we want mechanical
gates, not review vigilance.

- **Trust rule** (:func:`validate_preset_trust`): every server's
  ``command`` + ``args`` combination must match one of the four
  approved shapes:

    1. ``npx ... @modelcontextprotocol/<pkg>[@<tag>]``
    2. ``npx ... @playwright/<pkg>[@<tag>]``
    3. ``npx ... @github/<pkg>[@<tag>]``
    4. ``uvx --from git+https://github.com/kiso-run/<pkg>-mcp@<tag> <pkg>``

  Note that bare ``uvx <pypi-pkg>`` is **not** accepted — it would
  expose the preset to collisions with single-developer PyPI
  packages.

- **Single-key rule** (:func:`validate_preset_single_key`): the only
  ``${env:VAR}`` references allowed in preset ``args`` values or
  ``env`` blocks are :data:`ALLOWED_ENV_REFS`. Everything else is
  a violation — particularly other providers' API keys, which must
  route through OpenRouter in the default preset per the v0.10
  single-key invariant.
"""
from __future__ import annotations

import re


TRUST_ALLOWLIST: tuple[str, ...] = (
    "npx: @modelcontextprotocol/*",
    "npx: @playwright/*",
    "npx: @github/*",
    "uvx: git+https://github.com/kiso-run/*-mcp@<tag>",
)

ALLOWED_ENV_REFS: frozenset[str] = frozenset({
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "HOME",
    "KISO_DIR",
    "KISO_HOME",
})


_NPX_SCOPE_RE = re.compile(
    r"^@(modelcontextprotocol|playwright|github)/[a-z0-9][a-z0-9\-]*(@[^\s]+)?$"
)
_KISO_RUN_GIT_RE = re.compile(
    r"^git\+https://github\.com/kiso-run/[a-z0-9\-]+-mcp@[a-zA-Z0-9._\-+]+$"
)
_ENV_REF_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def validate_preset_trust(mcp_servers: dict[str, dict]) -> list[str]:
    """Return a list of violation strings (empty = passes)."""
    violations: list[str] = []
    for name, entry in mcp_servers.items():
        cmd = entry.get("command")
        args = entry.get("args") or []
        reason = _classify_trust(cmd, list(args))
        if reason is not None:
            violations.append(f"{name}: {reason}")
    return violations


def validate_preset_single_key(mcp_servers: dict[str, dict]) -> list[str]:
    """Return env-ref violations found in args + env blocks."""
    violations: list[str] = []
    for name, entry in mcp_servers.items():
        for arg in entry.get("args") or []:
            if not isinstance(arg, str):
                continue
            for var in _ENV_REF_RE.findall(arg):
                if var not in ALLOWED_ENV_REFS:
                    violations.append(
                        f"{name}: disallowed env ref ${{env:{var}}} in args"
                    )
        env_block = entry.get("env") or {}
        for key, value in env_block.items():
            if not isinstance(value, str):
                continue
            for var in _ENV_REF_RE.findall(value):
                if var not in ALLOWED_ENV_REFS:
                    violations.append(
                        f"{name}: disallowed env ref ${{env:{var}}} "
                        f"in env[{key}]"
                    )
    return violations


def _classify_trust(command: str | None, args: list[str]) -> str | None:
    """Return None if the (command, args) shape is allowed, else a reason."""
    if command == "npx":
        # Expect: npx [-y] <@scope/pkg[@tag]> [other args...]
        pkg = _first_package_arg(args)
        if pkg is None:
            return "npx invocation without a resolvable package argument"
        if not _NPX_SCOPE_RE.match(pkg):
            return (
                f"npx package {pkg!r} not in approved scopes "
                "(@modelcontextprotocol/*, @playwright/*, @github/*)"
            )
        return None

    if command == "uvx":
        # Accepted shape: uvx --from git+https://github.com/kiso-run/<x>-mcp@<tag> <entry>
        if "--from" not in args:
            return (
                "uvx invocation must use `--from git+...` with a "
                "kiso-run/*-mcp repo — bare `uvx <pkg>` is not allowed"
            )
        try:
            from_idx = args.index("--from")
            source = args[from_idx + 1]
        except (ValueError, IndexError):
            return "uvx --from requires a source argument"
        if not _KISO_RUN_GIT_RE.match(source):
            return (
                f"uvx --from source {source!r} is not a "
                "kiso-run/*-mcp repo pinned to a tag"
            )
        return None

    return f"command {command!r} is not on the approved allowlist"


def _first_package_arg(args: list[str]) -> str | None:
    """Return the first arg that looks like an npm package spec.

    Skips flags (``-y``, ``--yes``, etc.) and returns the first
    non-flag arg. Returns ``None`` if no package arg is found.
    """
    for arg in args:
        if isinstance(arg, str) and not arg.startswith("-"):
            return arg
    return None
