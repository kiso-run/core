"""Authentication: FastAPI dependency + user resolution."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request
from fastapi.exceptions import HTTPException

from kiso.config import Config


@dataclass(frozen=True)
class AuthInfo:
    """Result of token authentication."""
    token_name: str


@dataclass(frozen=True)
class ResolvedUser:
    """Result of user resolution."""
    username: str
    user: object  # config.User or None
    trusted: bool


async def require_auth(request: Request) -> AuthInfo:
    """FastAPI dependency: validate Bearer token, return AuthInfo or 401.

    Uses ``hmac.compare_digest`` for constant-time token comparison
    to prevent timing side-channel attacks.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token_value = auth[7:]
    config: Config = request.app.state.config
    for name, value in config.tokens.items():
        if hmac.compare_digest(value, token_value):
            return AuthInfo(token_name=name)
    raise HTTPException(status_code=401, detail="Invalid token")


def resolve_user(config: Config, user_field: str, token_name: str) -> ResolvedUser:
    """Resolve a user field to a ResolvedUser. Pure function.

    Three-step resolution:
    1. Direct username match in config.users
    2. Alias match: token_name:user_field matches a user's alias
    3. Untrusted: no match found
    """
    # 1. Direct username match
    if user_field in config.users:
        return ResolvedUser(
            username=user_field,
            user=config.users[user_field],
            trusted=True,
        )

    # 2. Alias match: look for token_name as connector, user_field as platform_id
    for uname, udata in config.users.items():
        if udata.aliases.get(token_name) == user_field:
            return ResolvedUser(username=uname, user=udata, trusted=True)

    # 3. Untrusted
    return ResolvedUser(username=user_field, user=None, trusted=False)
