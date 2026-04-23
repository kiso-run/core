"""Connector discovery (config-driven, server-side).

Kiso supervises external connector processes declared in
``config.toml`` under ``[connectors.<name>]`` sections. Kiso does NOT
install connector binaries; users bring their own (via ``uvx``, ``pip``,
``docker``, etc.) and declare a ``command`` + ``args`` for kiso to
spawn under supervision.

The supervisor's per-connector state files (``.pid``, ``.status.json``,
``connector.log``) live under ``~/.kiso/connectors/<name>/`` and are
created lazily the first time ``kiso connector start <name>`` runs.
The directory is NOT pre-created by ``kiso init``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

# Directory for supervisor state files (.pid, .status.json, logs).
# Not pre-created by init — lazily created when a connector starts.
CONNECTORS_DIR = KISO_DIR / "connectors"


def discover_connectors(config=None) -> list[dict]:
    """Return the connectors declared in ``config.toml``.

    Each dict carries the minimal shape that context-pool and sysenv
    consumers rely on: ``name``, ``description``, ``command``,
    ``enabled``. The ``description`` defaults to the first arg when the
    user did not provide one, mirroring what ``kiso mcp list`` shows.

    ``config`` is optional — when omitted, it is loaded from disk via
    ``load_config()``. Callers that already have a ``Config`` should
    pass it in to avoid re-reading ``config.toml``.
    """
    if config is None:
        try:
            from kiso.config import load_config

            config = load_config()
        except SystemExit:
            return []
    connectors: list[dict] = []
    for name in sorted(config.connectors.keys()):
        c = config.connectors[name]
        connectors.append(
            {
                "name": name,
                "description": _summary(c),
                "command": c.command,
                "args": list(c.args),
                "enabled": c.enabled,
            }
        )
    return connectors


def _summary(c) -> str:
    """Fallback description: command + first arg — matches ``kiso mcp list``."""
    if c.args:
        return f"{c.command} {c.args[0]}"
    return c.command
