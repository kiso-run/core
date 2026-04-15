"""OAuth 2.0 device authorization grant client (RFC 8628).

Used by MCP servers that require an interactive OAuth handshake
before their first ``tools/list`` call. Device flow is the right
fit for kiso's headless Docker runtime: the user opens a URL on
their own machine while kiso polls the auth server.

The module exposes two pure functions
(``request_device_code`` and ``poll_for_token``) plus exceptions.
The functions take the auth server URLs as parameters so the
same client works against GitHub, Google, Microsoft, or any
other RFC 8628 implementation.

Refresh-token handling is intentionally out of scope for v0.9 —
expired access tokens just trigger a re-run of the original
flow.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


__all__ = [
    "DeviceFlowError",
    "DeviceFlowExpired",
    "DeviceFlowResponse",
    "poll_for_token",
    "request_device_code",
]


class DeviceFlowError(Exception):
    """Generic device-flow failure (HTTP error, malformed response, etc.)."""


class DeviceFlowExpired(DeviceFlowError):
    """The device code expired before the user completed authorization."""


@dataclass(frozen=True)
class DeviceFlowResponse:
    """Parsed response from the device-code endpoint."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def request_device_code(
    *,
    device_code_url: str,
    client_id: str,
    scopes: str,
) -> DeviceFlowResponse:
    """Initiate a device authorization flow.

    *device_code_url* is the provider's device-code endpoint
    (e.g. ``https://github.com/login/device/code``). *scopes* is
    a space-separated scope list as accepted by the provider.

    Returns a :class:`DeviceFlowResponse` containing the device
    code, the user code to display, the verification URL, the
    overall expiry, and the recommended polling interval. Raises
    :class:`DeviceFlowError` on HTTP failure or malformed
    response.
    """
    try:
        resp = httpx.post(
            device_code_url,
            data={"client_id": client_id, "scope": scopes},
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise DeviceFlowError(
            f"device code request failed: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise DeviceFlowError(
            f"device code request returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise DeviceFlowError(
            f"device code response is not JSON: {exc}"
        ) from exc

    try:
        return DeviceFlowResponse(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            expires_in=int(body.get("expires_in", 900)),
            interval=int(body.get("interval", 5)),
        )
    except KeyError as exc:
        raise DeviceFlowError(
            f"device code response missing required field: {exc}"
        ) from exc


def poll_for_token(
    *,
    token_url: str,
    client_id: str,
    device_code: str,
    interval: int,
    expires_in: int,
) -> dict:
    """Poll the token endpoint until success, expiry, or denial.

    Returns the parsed JSON response on success (containing at
    least ``access_token`` and ``token_type``). Raises
    :class:`DeviceFlowExpired` when the device code expires.
    Raises :class:`DeviceFlowError` for any other terminal error
    (``access_denied``, ``unsupported_grant_type``, HTTP failure).

    Implements the RFC 8628 §3.5 polling rules:
    - ``authorization_pending``: sleep and retry
    - ``slow_down``: increase interval by 5s and retry
    - ``access_denied`` / ``expired_token`` / unknown error:
      raise terminal error
    """
    deadline_marker = time.monotonic() + expires_in
    current_interval = interval

    while True:
        if time.monotonic() >= deadline_marker:
            raise DeviceFlowExpired(
                f"device code expired before user completed authorization "
                f"(after {expires_in}s)"
            )

        time.sleep(current_interval)

        try:
            resp = httpx.post(
                token_url,
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise DeviceFlowError(
                f"token poll request failed: {exc}"
            ) from exc

        if resp.status_code >= 500:
            raise DeviceFlowError(
                f"token endpoint returned HTTP {resp.status_code}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise DeviceFlowError(
                f"token response is not JSON: {exc}"
            ) from exc

        if "access_token" in body:
            return body

        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            # RFC 8628 §3.5 — increase interval by at least 5s
            current_interval += 5
            continue
        if error == "expired_token":
            raise DeviceFlowExpired(
                "device code expired before user completed authorization"
            )
        raise DeviceFlowError(
            f"device flow terminal error: {error or 'unknown'} — "
            f"{body.get('error_description', '')}"
        )
