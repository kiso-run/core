"""Shared fixtures and helpers for CLI user management tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w

_MINIMAL_USERS = {
    "boss": {"role": "admin"},
    "alice": {"role": "user", "skills": ["skill1", "skill2"]},
}

_MINIMAL_CONFIG = {
    "tokens": {"cli": "test-token"},
    "providers": {"openrouter": {"base_url": "https://example.com"}},
    "models": {
        "planner": "m", "reviewer": "m", "curator": "m", "worker": "m",
        "summarizer": "m", "paraphraser": "m", "messenger": "m", "searcher": "m",
    },
    "settings": {},
}


def make_user_config(tmp_path: Path, users: dict | None = None) -> Path:
    """Write a minimal config.toml with the given users (defaults to _MINIMAL_USERS)."""
    p = tmp_path / "config.toml"
    raw = {**_MINIMAL_CONFIG, "users": users if users is not None else _MINIMAL_USERS}
    with open(p, "wb") as f:
        tomli_w.dump(raw, f)
    return p


def read_users(path: Path) -> dict:
    """Read the users section from a config.toml file."""
    with open(path, "rb") as f:
        return tomllib.load(f)["users"]
