"""Shared utilities for skill and connector CLI operations."""

from __future__ import annotations

import getpass
import os
import re
import sys

from kiso.config import KISO_DIR, load_config

OFFICIAL_ORG = "kiso-run"
REGISTRY_URL = "https://raw.githubusercontent.com/kiso-run/core/main/registry.json"

# Prevent git from opening /dev/tty to prompt for credentials.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def url_to_name(url: str) -> str:
    """Convert a git URL to a plugin install name.

    Algorithm (from docs/skills.md):
    1. Strip .git suffix
    2. Normalize SSH (git@host:ns/repo -> host/ns/repo) and HTTPS (strip scheme)
    3. Lowercase
    4. Replace . with - (domain)
    5. Replace / with _
    """
    name = url
    if name.endswith(".git"):
        name = name[:-4]
    if name.startswith("git@"):
        name = name[4:]
        name = name.replace(":", "/", 1)
    name = re.sub(r"^https?://", "", name)
    name = name.lower()
    name = name.replace(".", "-")
    name = name.replace("/", "_")
    return name


def is_url(target: str) -> bool:
    """Return True if target looks like a git URL."""
    return target.startswith(("git@", "http://", "https://"))


def is_repo_not_found(stderr: str) -> bool:
    """Detect git clone failures caused by a nonexistent repo.

    Git outputs different messages depending on auth setup:
    - "not found" — when the server explicitly returns 404
    - "terminal prompts disabled" — when GIT_TERMINAL_PROMPT=0 and repo
      doesn't exist (GitHub returns a credential challenge for 404s)
    """
    s = stderr.lower()
    return "not found" in s or "terminal prompts disabled" in s


def require_admin() -> None:
    """Check that the current Linux user is an admin in kiso config. Exits 1 if not."""
    username = getpass.getuser()
    cfg = load_config()
    user = cfg.users.get(username)
    if user is None:
        print(f"error: unknown user '{username}'")
        sys.exit(1)
    if user.role != "admin":
        print(f"error: user '{username}' is not an admin")
        sys.exit(1)


def fetch_registry() -> dict:
    """Fetch the official registry from GitHub (raw file, no API)."""
    import json

    import httpx

    try:
        resp = httpx.get(REGISTRY_URL, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        return json.loads(resp.text)
    except httpx.HTTPError as exc:
        print(f"error: failed to fetch registry: {exc}")
        sys.exit(1)
    except (json.JSONDecodeError, KeyError):
        print("error: invalid registry format")
        sys.exit(1)


def search_entries(entries: list[dict], query: str | None) -> list[dict]:
    """Filter registry entries: match name first, then description."""
    if not query:
        return entries
    q = query.lower()
    by_name = [e for e in entries if q in e["name"].lower()]
    if by_name:
        return by_name
    return [e for e in entries if q in e.get("description", "").lower()]
