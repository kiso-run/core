"""Unit tests for _RateLimiter (90a) and endpoint rate limits."""

from __future__ import annotations

import httpx
import pytest

from kiso.main import _RateLimiter, _rate_limiter
from tests.conftest import AUTH_HEADER


# ── _RateLimiter unit tests ────────────────────────────────────────────────────


class TestRateLimiter:
    @pytest.fixture(autouse=True)
    def fresh_limiter(self):
        """Use a fresh instance per test (not the global singleton)."""
        self.rl = _RateLimiter()

    async def test_allows_requests_within_limit(self):
        """All requests within the limit are allowed."""
        for _ in range(5):
            allowed = await self.rl.check("key", limit=5)
            assert allowed is True

    async def test_blocks_request_exceeding_limit(self):
        """The (limit+1)-th request is denied."""
        for _ in range(5):
            await self.rl.check("key", limit=5)
        blocked = await self.rl.check("key", limit=5)
        assert blocked is False

    async def test_keys_are_independent(self):
        """Exhausting one key does not affect another key."""
        for _ in range(3):
            await self.rl.check("a", limit=3)
        # "a" is now exhausted; "b" should still be fresh
        assert await self.rl.check("a", limit=3) is False
        assert await self.rl.check("b", limit=3) is True

    async def test_reset_clears_all_buckets(self):
        """reset() restores full capacity for all keys."""
        for _ in range(5):
            await self.rl.check("key", limit=5)
        assert await self.rl.check("key", limit=5) is False

        self.rl.reset()
        assert await self.rl.check("key", limit=5) is True

    async def test_first_request_always_allowed(self):
        """Brand-new key starts with a full bucket."""
        assert await self.rl.check("new-key", limit=1) is True

    async def test_limit_one_blocks_second_request(self):
        """With limit=1 the second request is always denied."""
        assert await self.rl.check("k", limit=1) is True
        assert await self.rl.check("k", limit=1) is False


# ── /msg rate limiting (90a) ───────────────────────────────────────────────────


async def test_msg_rate_limited_after_20(client: httpx.AsyncClient):
    """POST /msg returns 429 for the 21st request from the same user."""
    _rate_limiter.reset()

    # 20 allowed requests
    for _ in range(20):
        r = await client.post(
            "/msg",
            json={"session": "rl-sess", "user": "testuser", "content": "hi"},
            headers=AUTH_HEADER,
        )
        assert r.status_code in (202, 429), f"unexpected {r.status_code}"
        if r.status_code == 429:
            pytest.fail("Rate limit hit before 20 requests")

    # 21st should be rate-limited
    resp = await client.post(
        "/msg",
        json={"session": "rl-sess", "user": "testuser", "content": "hi"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 429
    assert "Rate limit exceeded" in resp.json()["detail"]


async def test_msg_rate_limit_is_per_user(client: httpx.AsyncClient):
    """Rate limit on /msg is per user: exhausting one user does not block another."""
    _rate_limiter.reset()

    for _ in range(20):
        await client.post(
            "/msg",
            json={"session": "rl-sess", "user": "testuser", "content": "hi"},
            headers=AUTH_HEADER,
        )

    # testadmin is a separate key and should still be allowed
    resp = await client.post(
        "/msg",
        json={"session": "rl-sess", "user": "testadmin", "content": "hi"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code != 429
