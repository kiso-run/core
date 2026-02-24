"""Webhook URL validation (SSRF prevention) and delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# Retry delays (seconds) for webhook delivery: 1s → 3s → 9s
_WEBHOOK_BACKOFF = [1, 3, 9]


def validate_webhook_url(
    url: str,
    allow_list: list[str] | None = None,
    require_https: bool = True,
) -> None:
    """Validate a webhook URL. Raises ValueError on rejection.

    Checks:
    - Scheme must be https (or http if require_https is False)
    - Hostname must resolve to a non-private IP (unless in allow_list)
    """
    allow_list = allow_list or []

    parsed = urlparse(url)
    if require_https:
        if parsed.scheme != "https":
            raise ValueError(
                f"Webhook URL must use https (got '{parsed.scheme}'). "
                "Set webhook_require_https = false in config to allow http."
            )
    elif parsed.scheme not in ("http", "https"):
        raise ValueError(f"Webhook URL scheme must be http or https, got '{parsed.scheme}'")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL has no hostname")

    # Resolve hostname to IPs
    try:
        addrinfos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve webhook hostname '{hostname}': {e}")

    if not addrinfos:
        raise ValueError(f"Webhook hostname '{hostname}' did not resolve to any address")

    for family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if ip_str in allow_list:
            continue
        addr = ipaddress.ip_address(ip_str)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(
                f"Webhook URL resolves to private/reserved address {ip_str}"
            )


async def deliver_webhook(
    url: str,
    session: str,
    task_id: int,
    content: str,
    final: bool,
    secret: str = "",
    max_payload: int = 0,
) -> tuple[bool, int, int]:
    """POST webhook payload. Retries 3 times with backoff.

    Returns (success, last_status_code, attempts).
    Never raises — logs warning on all failures and returns (False, ...).

    HTTP redirects are explicitly disabled (``follow_redirects=False``) to
    prevent SSRF via redirect to private/internal IPs that would bypass the
    URL validation performed by ``validate_webhook_url``.
    """
    # Truncate content if needed
    if max_payload > 0 and len(content.encode()) > max_payload:
        marker = " [truncated]"
        cut_at = max(0, max_payload - len(marker.encode()))
        content = content.encode()[:cut_at].decode(errors="ignore") + marker

    payload = {
        "session": session,
        "task_id": task_id,
        "type": "msg",
        "content": content,
        "final": final,
    }
    raw_body = json.dumps(payload).encode()

    headers = {"Content-Type": "application/json"}
    if secret:
        sig = hmac_mod.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        headers["X-Kiso-Signature"] = f"sha256={sig}"

    last_status = 0

    for attempt, delay in enumerate(_WEBHOOK_BACKOFF):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                resp = await client.post(url, content=raw_body, headers=headers)
            last_status = resp.status_code
            if resp.status_code < 400:
                return True, resp.status_code, attempt + 1
            log.warning(
                "Webhook attempt %d/%d to %s returned %d",
                attempt + 1, len(_WEBHOOK_BACKOFF), url, resp.status_code,
            )
        except Exception as e:
            log.warning(
                "Webhook attempt %d/%d to %s failed: %s",
                attempt + 1, len(_WEBHOOK_BACKOFF), url, e,
            )

        if attempt < len(_WEBHOOK_BACKOFF) - 1:
            await asyncio.sleep(delay)

    log.warning("All webhook delivery attempts failed for %s", url)
    return False, last_status, len(_WEBHOOK_BACKOFF)
