"""Install-time trust check for MCP server sources.

Tier 1 (hardcoded): sources maintained by the MCP ecosystem owners
or by kiso-run itself. These install silently.

Tier 2 (custom): user-added prefixes in ``~/.kiso/trust.json``.

Anything else is untrusted — the CLI warns and requires explicit
approval (``--yes`` or an interactive prompt).
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from kiso.trust_store import load_trust_store, matches_any_prefix


# Keep synced with kiso/trust_rules.py:TRUST_ALLOWLIST; that module
# gates the shipped default preset, this one gates runtime install.
MCP_TIER1_PREFIXES: tuple[str, ...] = (
    "npm:@modelcontextprotocol/",
    "npm:@playwright/",
    "npm:@github/",
    "github.com/kiso-run/",
)


TrustTier = Literal["tier1", "custom", "untrusted"]


def is_trusted(source_key: str) -> TrustTier:
    """Classify *source_key* into a trust tier.

    *source_key* is the normalised form produced by
    :func:`source_key_for_url` — e.g. ``npm:@org/pkg`` or
    ``github.com/owner/repo`` — so the prefix table is plain
    strings rather than URL-regex.
    """
    if matches_any_prefix(source_key, MCP_TIER1_PREFIXES):
        return "tier1"
    custom = load_trust_store().mcp
    if matches_any_prefix(source_key, custom):
        return "custom"
    return "untrusted"


def source_key_for_url(url: str) -> str:
    """Normalise an MCP install URL into a trust-prefix shape.

    Accepts every form :func:`kiso.mcp.install.resolve_from_url`
    accepts. Falls back to returning the URL unchanged when no
    known host matches — the caller's is_trusted check will then
    simply report ``untrusted``.
    """
    if url.startswith("npm:") or url.startswith("pypi:"):
        return url

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if host.endswith("npmjs.com") and "/package/" in path:
        pkg = path.split("/package/", 1)[1].rstrip("/")
        return f"npm:{pkg}"
    if host.endswith("pypi.org") and "/project/" in path:
        pkg = path.split("/project/", 1)[1].strip("/")
        return f"pypi:{pkg}"
    if host.endswith("github.com"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return f"github.com/{parts[0]}/{parts[1].removesuffix('.git')}"

    return url
