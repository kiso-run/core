"""Shared helpers for ``~/.kiso/mcp/<name>.env`` credential files.

The CLI (``cli/mcp.py``) writes and reads these files; the stdio
client reads them at spawn time to merge KEY=VAL pairs into the
subprocess env. Parsing lives here so both paths stay in sync.
"""

from __future__ import annotations

from pathlib import Path

from kiso import config as _config


def env_file_path(server_name: str) -> Path:
    return _config.KISO_DIR / "mcp" / f"{server_name}.env"


def parse_env_file_text(text: str) -> dict[str, str]:
    """Parse KEY=VAL lines from an env-file body.

    Skips blanks and ``#`` comments. Splits on the first ``=`` so
    values containing ``=`` are preserved verbatim. Returns an empty
    dict for empty input.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value
    return out
