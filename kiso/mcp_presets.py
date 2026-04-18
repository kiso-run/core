"""MCP preset loader and ``mcpServers`` → ``[mcp.<name>]`` TOML renderer.

The preset is the bootstrap-only file consumed by
``kiso init --preset <name>``. It ships in the repo at
``kiso/presets/<name>.mcp.json`` and follows the standard
``mcpServers`` shape so the same file could be dropped into any MCP
client. After ``init``, kiso never reads the preset again — users
edit their own ``config.toml``.

Runtime format divergence: kiso stores MCP servers as
``[mcp.<name>]`` TOML sections (see :mod:`kiso.mcp.config`). This
module bridges the two shapes so ``init`` can produce a
config.toml that kiso's own parser will accept.
"""
from __future__ import annotations

import json
from pathlib import Path

import tomli_w


MCP_PRESETS_DIR = Path(__file__).parent / "presets"


def list_mcp_presets() -> list[str]:
    """Return the names of presets shipped in this package.

    Looks for ``<name>.mcp.json`` files in :data:`MCP_PRESETS_DIR`.
    """
    if not MCP_PRESETS_DIR.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(MCP_PRESETS_DIR.glob("*.mcp.json")):
        names.append(entry.name.removesuffix(".mcp.json"))
    return names


def load_mcp_preset(name: str) -> dict:
    """Load a preset JSON file by bare name.

    Returns the full parsed document (including ``$schema`` /
    ``$comment`` / ``mcpServers``).

    Raises :class:`FileNotFoundError` if the preset does not exist.
    """
    path = MCP_PRESETS_DIR / f"{name}.mcp.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"preset {name!r} not found at {path}; "
            f"available: {list_mcp_presets()}"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def preset_mcp_servers(preset_data: dict) -> dict[str, dict]:
    """Extract the ``mcpServers`` block from a loaded preset."""
    servers = preset_data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError(
            "preset is missing the 'mcpServers' block or it is not an object"
        )
    return servers


def render_mcp_toml(mcp_servers: dict[str, dict]) -> str:
    """Render an ``mcpServers`` block as kiso's TOML shape.

    Each entry becomes a ``[mcp.<name>]`` table with
    ``transport = "stdio"`` (the preset format doesn't name the
    transport, but MCP's local-process convention is stdio). Args
    and env carry over untouched — including ``${env:VAR}``
    references, which kiso's :mod:`kiso.mcp.config` parser resolves
    at config-load time.
    """
    document: dict[str, dict] = {"mcp": {}}
    for name, entry in mcp_servers.items():
        if not isinstance(entry, dict):
            raise ValueError(f"mcpServers[{name!r}] is not an object")
        toml_section: dict[str, object] = {"transport": "stdio"}
        if "command" in entry:
            toml_section["command"] = entry["command"]
        if "args" in entry and entry["args"]:
            toml_section["args"] = list(entry["args"])
        if "env" in entry and entry["env"]:
            toml_section["env"] = dict(entry["env"])
        document["mcp"][name] = toml_section
    return tomli_w.dumps(document)
