"""Interactive end-to-end test for the MCP OAuth device flow (M1372).

Exercises ``kiso.mcp.oauth`` + ``kiso.mcp.credentials`` against
real GitHub. Requires a human at the terminal because OAuth
device flow needs the user to open a URL and paste a code in
their browser. This test is **never** executed by CI or by
``./utils/run_tests.sh a``; it lives under ``tests/interactive/``
and is gated by the existing ``--interactive`` flag (see
``tests/conftest.py:45``).

How to run::

    KISO_OAUTH_GITHUB_CLIENT_ID=<your_oauth_app_client_id> \\
    uv run pytest tests/interactive/test_mcp_oauth_github.py \\
        --interactive -s -v

You need a GitHub OAuth App (Settings → Developer settings →
OAuth Apps) with device flow enabled. Only the *Client ID* is
needed — device flow does not use the client secret.

What the test verifies (vertical, end-to-end):

1. ``request_device_code`` returns a real ``device_code`` /
   ``user_code`` / verification URL from GitHub.
2. The test prints the URL and code to the terminal and waits.
3. The user opens the URL in a browser and authorises the app.
4. ``poll_for_token`` returns a real ``access_token``.
5. ``save_credential`` persists the token under
   ``~/.kiso/mcp/credentials/github_test.json``.
6. ``load_credential`` reads it back identically — the round-trip
   contract holds against a real provider response.
7. The credential file is mode ``0600``.

This is the only test in the repository that exercises the full
auth handshake against a live provider. It is intentionally
slow and intentionally not in CI — its purpose is to *prove*
the contract once during a manual release qualification, not to
gate every commit.
"""

from __future__ import annotations

import os
import stat

import pytest

from kiso.mcp.credentials import (
    delete_credential,
    load_credential,
    save_credential,
    server_credentials_path,
)
from kiso.mcp.oauth import (
    poll_for_token,
    request_device_code,
)


pytestmark = [pytest.mark.interactive]


_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_TEST_SERVER_NAME = "github_test"


@pytest.fixture(autouse=True)
def _cleanup_test_credentials():
    """Always start from a clean slate and tidy up afterwards."""
    delete_credential(_TEST_SERVER_NAME)
    yield
    delete_credential(_TEST_SERVER_NAME)


def test_github_device_flow_round_trip() -> None:
    client_id = os.environ.get("KISO_OAUTH_GITHUB_CLIENT_ID")
    if not client_id:
        pytest.skip(
            "KISO_OAUTH_GITHUB_CLIENT_ID env var not set — create a "
            "GitHub OAuth App with device flow enabled and export its "
            "Client ID to run this test"
        )

    print()
    print("=" * 70)
    print("  M1372 — GitHub OAuth device flow round-trip test")
    print("=" * 70)
    print()
    print("Requesting device code from GitHub...")

    device_response = request_device_code(
        device_code_url=_GITHUB_DEVICE_CODE_URL,
        client_id=client_id,
        scopes="read:user",
    )

    print()
    print("ACTION REQUIRED:")
    print(f"  1. Open this URL in your browser: {device_response.verification_uri}")
    print(f"  2. Enter this code: {device_response.user_code}")
    print(f"  3. Authorise the OAuth app")
    print()
    print(f"Polling for token (interval={device_response.interval}s, "
          f"expires in {device_response.expires_in}s)...")
    print()

    token = poll_for_token(
        token_url=_GITHUB_TOKEN_URL,
        client_id=client_id,
        device_code=device_response.device_code,
        interval=device_response.interval,
        expires_in=device_response.expires_in,
    )

    assert "access_token" in token, "token response missing access_token"
    assert token.get("token_type", "").lower() == "bearer"
    print(f"✓ Got access token (type={token['token_type']})")

    save_credential(_TEST_SERVER_NAME, token)
    print(f"✓ Saved credential to {server_credentials_path(_TEST_SERVER_NAME)}")

    loaded = load_credential(_TEST_SERVER_NAME)
    assert loaded is not None
    assert loaded["access_token"] == token["access_token"]
    assert loaded["token_type"] == token["token_type"]
    assert "saved_at" in loaded
    print("✓ Round-trip: load_credential returned the saved token verbatim")

    cred_path = server_credentials_path(_TEST_SERVER_NAME)
    mode = stat.S_IMODE(cred_path.stat().st_mode)
    assert mode == 0o600, (
        f"credential file must be mode 0600 (no group/other access), "
        f"got {oct(mode)}"
    )
    print(f"✓ File mode is {oct(mode)}")

    print()
    print("=" * 70)
    print("  M1372 round-trip PASSED")
    print("=" * 70)
