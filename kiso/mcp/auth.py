"""Pre-connection auth resolution for MCP servers.

Called by transport clients (stdio, HTTP) before ``initialize()``
to ensure the server's auth requirements are met. When a server
declares ``auth = { type = "device_flow", ... }``, this module
consults the credential store for a cached token and falls back
to an interactive device flow if none is available or expired.

The function ``resolve_auth`` is sync because the underlying
primitives (``kiso.mcp.credentials`` file I/O and
``kiso.mcp.oauth`` httpx sync calls) are all sync.
"""

from __future__ import annotations

import logging
import sys
import time

from kiso.mcp.config import MCPConfigError, MCPServer
from kiso.mcp.credentials import load_credential, save_credential
from kiso.mcp.oauth import (
    DeviceFlowError,
    request_device_code,
    poll_for_token,
)

log = logging.getLogger(__name__)

_SUPPORTED_AUTH_TYPES = frozenset({"device_flow"})


def resolve_auth(server: MCPServer) -> str | None:
    """Resolve auth for *server*, returning an access token or None.

    Returns ``None`` when no auth is configured. Returns the
    ``access_token`` string when a valid credential is found in the
    store or obtained via an interactive device flow. Raises
    ``MCPConfigError`` for unsupported auth types or missing required
    config fields.
    """
    auth = server.auth
    if not auth or not isinstance(auth, dict):
        return None
    auth_type = auth.get("type")
    if not auth_type:
        return None

    if auth_type not in _SUPPORTED_AUTH_TYPES:
        raise MCPConfigError(
            f"[mcp.{server.name}]: unsupported auth type {auth_type!r}; "
            f"supported: {sorted(_SUPPORTED_AUTH_TYPES)}"
        )

    return _resolve_device_flow(server.name, auth)


def _resolve_device_flow(server_name: str, auth: dict) -> str:
    """Resolve device-flow auth for a server.

    Checks the credential store first. If a valid (non-expired)
    token exists, returns it. Otherwise runs the interactive
    device flow, persists the result, and returns the new token.
    """
    client_id = auth.get("client_id")
    if not client_id:
        raise MCPConfigError(
            f"[mcp.{server_name}]: device_flow auth requires 'client_id'"
        )
    device_code_url = auth.get("device_code_url")
    if not device_code_url:
        raise MCPConfigError(
            f"[mcp.{server_name}]: device_flow auth requires 'device_code_url'"
        )
    token_url = auth.get("token_url")
    if not token_url:
        raise MCPConfigError(
            f"[mcp.{server_name}]: device_flow auth requires 'token_url'"
        )
    scopes = auth.get("scopes", "")

    # Check credential store for a cached token
    cached = load_credential(server_name)
    if cached and _is_valid(cached):
        log.info("mcp[%s]: reusing cached auth token", server_name)
        return cached["access_token"]

    # Run the interactive device flow
    log.info("mcp[%s]: initiating device flow", server_name)
    try:
        dfr = request_device_code(
            device_code_url=device_code_url,
            client_id=client_id,
            scopes=scopes,
        )
    except DeviceFlowError as exc:
        raise MCPConfigError(
            f"[mcp.{server_name}]: device code request failed: {exc}"
        ) from exc

    # Print user instructions to stderr (not stdout, which is
    # reserved for the MCP protocol in stdio transport).
    print(
        f"\n{'=' * 60}\n"
        f"  MCP server '{server_name}' requires authorization\n"
        f"{'=' * 60}\n"
        f"\n"
        f"  1. Open: {dfr.verification_uri}\n"
        f"  2. Enter code: {dfr.user_code}\n"
        f"  3. Authorize the app\n"
        f"\n"
        f"  Polling (interval={dfr.interval}s, "
        f"expires in {dfr.expires_in}s)...\n",
        file=sys.stderr,
        flush=True,
    )

    try:
        token = poll_for_token(
            token_url=token_url,
            client_id=client_id,
            device_code=dfr.device_code,
            interval=dfr.interval,
            expires_in=dfr.expires_in,
        )
    except DeviceFlowError as exc:
        raise MCPConfigError(
            f"[mcp.{server_name}]: device flow failed: {exc}"
        ) from exc

    save_credential(server_name, token)
    log.info("mcp[%s]: auth token obtained and saved", server_name)
    return token["access_token"]


def _is_valid(credential: dict) -> bool:
    """Check whether a cached credential is still valid (not expired).

    A credential without ``expires_in`` is assumed valid indefinitely
    (the caller re-runs the flow on 401 errors). A credential with
    ``expires_in`` is valid if ``saved_at + expires_in > now``.
    """
    if "access_token" not in credential:
        return False
    expires_in = credential.get("expires_in")
    if expires_in is None:
        return True
    saved_at = credential.get("saved_at", 0)
    return (saved_at + expires_in) > time.time()
