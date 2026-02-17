"""Webhook URL validation (SSRF prevention) and delivery."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


def validate_webhook_url(url: str, allow_list: list[str] | None = None) -> None:
    """Validate a webhook URL. Raises ValueError on rejection.

    Checks:
    - Scheme must be http or https
    - Hostname must resolve to a non-private IP (unless in allow_list)
    """
    allow_list = allow_list or []

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
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
) -> bool:
    """POST webhook payload. Retries 3 times with backoff. Returns True on success.

    Never raises — logs warning on all failures and returns False.
    """
    payload = {
        "session": session,
        "task_id": task_id,
        "type": "msg",
        "content": content,
        "final": final,
    }
    backoff = [1, 3, 9]

    for attempt, delay in enumerate(backoff):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code < 400:
                return True
            log.warning(
                "Webhook attempt %d/%d to %s returned %d",
                attempt + 1, len(backoff), url, resp.status_code,
            )
        except Exception as e:
            log.warning(
                "Webhook attempt %d/%d to %s failed: %s",
                attempt + 1, len(backoff), url, e,
            )

        if attempt < len(backoff) - 1:
            await asyncio.sleep(delay)

    log.warning("All webhook delivery attempts failed for %s", url)
    return False
