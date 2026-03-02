"""Shared HTTP helpers for CLI commands."""

from __future__ import annotations

import sys


def _cli_request(method: str, args, path: str, params: dict | None = None):
    """Make an authenticated request to the kiso server.

    Loads the 'cli' token from config, attaches it as a Bearer header,
    and handles ConnectError and HTTPStatusError by printing to stderr
    and calling sys.exit(1).

    Returns the httpx.Response on success.
    """
    import httpx

    from kiso.config import load_config

    cfg = load_config()
    token = cfg.tokens.get("cli")
    if not token:
        print("error: no 'cli' token in config.toml", file=sys.stderr)
        sys.exit(1)

    url = f"{args.api}{path}"
    try:
        resp = httpx.request(
            method,
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        print(f"error: cannot connect to {args.api}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        print(f"error: {exc.response.status_code} — {exc.response.text}", file=sys.stderr)
        sys.exit(1)

    return resp


def cli_get(args, path: str, params: dict | None = None):
    """Authenticated GET request to the kiso server. Exits on error."""
    return _cli_request("GET", args, path, params)


def cli_post(args, path: str, params: dict | None = None):
    """Authenticated POST request to the kiso server. Exits on error."""
    return _cli_request("POST", args, path, params)
