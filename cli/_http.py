"""Shared HTTP helpers for CLI commands."""

from __future__ import annotations

import sys


def _handle_http_error(exc, api_url: str, *, fatal: bool = True) -> None:
    """Print an HTTP error to stderr and optionally exit.

    *fatal=True* (default) calls ``sys.exit(1)`` after printing.
    *fatal=False* prints the error and returns so the caller can
    ``continue`` or ``return``.
    """
    import httpx

    if isinstance(exc, httpx.ConnectError):
        msg = f"cannot connect to {api_url}"
    elif isinstance(exc, httpx.HTTPStatusError):
        msg = f"{exc.response.status_code} — {exc.response.text}"
    else:
        msg = str(exc)

    print(f"error: {msg}", file=sys.stderr)
    if fatal:
        sys.exit(1)


def _cli_request(
    method: str, args, path: str,
    params: dict | None = None,
    json_body: dict | None = None,
):
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
            json=json_body,
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


def cli_post(args, path: str, params: dict | None = None, json_body: dict | None = None):
    """Authenticated POST request to the kiso server. Exits on error."""
    return _cli_request("POST", args, path, params, json_body=json_body)


def cli_delete(args, path: str, params: dict | None = None):
    """Authenticated DELETE request to the kiso server. Exits on error."""
    return _cli_request("DELETE", args, path, params)
