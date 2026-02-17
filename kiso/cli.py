"""CLI entry point."""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: kiso <command>")
        print("commands: serve")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "serve":
        _serve()
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)


def _serve() -> None:
    import uvicorn

    from kiso.config import SETTINGS_DEFAULTS, load_config

    # Load config early to fail fast on errors
    cfg = load_config()
    host = cfg.settings.get("host", SETTINGS_DEFAULTS["host"])
    port = cfg.settings.get("port", SETTINGS_DEFAULTS["port"])

    uvicorn.run("kiso.main:app", host=str(host), port=int(port))


if __name__ == "__main__":
    main()
