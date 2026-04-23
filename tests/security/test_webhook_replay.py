"""Concern 5 — signed webhook payloads must carry a timestamp.

HMAC alone proves the sender knew the secret, but does nothing
against replay: a captured request remains valid forever. Kiso's
webhook contract therefore includes a ``sent_at`` Unix timestamp
inside the signed payload so a verifier can reject stale deliveries
(e.g. older than 5 minutes).

The runtime side of the invariant:
- Signed payloads always include a ``sent_at`` integer field.
- The field is INSIDE the signed body, so tampering with it
  invalidates the HMAC signature.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import time
from typing import Any

import httpx
import pytest

from kiso import webhook as webhook_mod


pytestmark = pytest.mark.asyncio


class _Capture:
    def __init__(self):
        self.body: bytes | None = None
        self.headers: dict[str, str] | None = None

    async def post(self, url, content=None, headers=None):  # noqa: ARG002
        self.body = bytes(content or b"")
        self.headers = dict(headers or {})

        class _Resp:
            status_code = 200
        return _Resp()


class _Client:
    def __init__(self, capture: _Capture):
        self._capture = capture

    async def __aenter__(self):
        return self._capture

    async def __aexit__(self, *a):
        return False


async def test_signed_payload_includes_sent_at_timestamp(monkeypatch):
    capture = _Capture()

    def client_factory(*a, **kw):  # noqa: ARG001
        return _Client(capture)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    secret = "shared-secret"
    before = int(time.time())
    ok, status, attempts = await webhook_mod.deliver_webhook(
        url="https://example.com/hook",
        session="s1",
        task_id=1,
        content="hello",
        final=True,
        secret=secret,
    )
    after = int(time.time())
    assert ok is True
    assert status == 200
    assert attempts == 1

    # Body is the signed JSON; parse and check timestamp.
    assert capture.body is not None
    payload = json.loads(capture.body.decode())
    assert "sent_at" in payload, payload
    assert isinstance(payload["sent_at"], int)
    assert before <= payload["sent_at"] <= after

    # HMAC signature covers the body that includes sent_at —
    # recomputing it must match what the runtime sent.
    assert capture.headers is not None
    sig_header = capture.headers.get("X-Kiso-Signature")
    assert sig_header and sig_header.startswith("sha256=")
    expected = hmac_mod.new(
        secret.encode(), capture.body, hashlib.sha256
    ).hexdigest()
    assert sig_header == f"sha256={expected}"
