"""Shared CLI helpers: admin gate + subcommand dispatcher.

Extracted from the now-retired ``cli/plugin_ops.py`` when the connector
plugin-install subsystem was removed. Other CLI modules still need a
common way to check admin status (``kiso cron/env/config/behavior/...``)
and dispatch subcommands from argparse output.
"""

from __future__ import annotations

import getpass
import os
import sys


def require_admin() -> None:
    """Exit with code 1 unless the current Linux user is an admin in kiso config."""
    from kiso.config import load_config

    username = getpass.getuser()
    if username == "root" and os.getuid() == 0:
        return  # running inside the kiso container as root — skip check

    cfg = load_config()
    user = cfg.users.get(username)
    if user is None:
        print(f"error: unknown user '{username}'")
        sys.exit(1)
    if user.role != "admin":
        print(f"error: user '{username}' is not an admin")
        sys.exit(1)


def dispatch_subcommand(
    args: object, attr: str, handlers: dict, usage: str,
) -> None:
    """Dispatch a CLI subcommand to its handler.

    Reads ``getattr(args, attr)`` and calls the matching handler.
    Falls back to printing *usage* and exiting with code 1.
    """
    cmd = getattr(args, attr, None)
    if cmd is None or cmd not in handlers:
        print(usage)
        sys.exit(1)
    handlers[cmd](args)
