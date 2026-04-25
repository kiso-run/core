"""Install-time trust + local risk-factor detection for Agent Skills.

Tier 1 (hardcoded): skills from Anthropic or kiso-run curated repos.

Tier 2 (custom): user-added prefixes in ``~/.kiso/trust.json``.

Risk factors are independent from trust — they surface reasons to
look carefully at the install (presence of ``scripts/``, wide
``allowed-tools``, oversized assets) but don't themselves block.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from kiso.trust_store import load_trust_store, matches_any_prefix


SKILL_TIER1_PREFIXES: tuple[str, ...] = (
    "github.com/anthropics/skills/",
    "github.com/kiso-run/skills/",
    # Single-skill repos under the kiso-run org. Each is its own
    # release-tagged GitHub repo; we list them explicitly rather
    # than wildcard the owner so a stray `kiso-run/<random>-skill`
    # repo can't silently mark itself trusted.
    "github.com/kiso-run/message-attachment-receiver-skill",
)


TrustTier = Literal["tier1", "custom", "untrusted"]


_MAX_SKILL_MD_BYTES = 50 * 1024
_MAX_ASSETS_BYTES = 5 * 1024 * 1024
_RISKY_TOOL_RE = re.compile(
    r"Bash\(\s*\*\s*\)|Write\(\s*\*\s*\)|Edit\(\s*\*\s*\)",
    re.IGNORECASE,
)


def is_trusted(source_key: str) -> TrustTier:
    """Classify *source_key* (e.g. ``github.com/acme/writing-style``)."""
    if matches_any_prefix(source_key, SKILL_TIER1_PREFIXES):
        return "tier1"
    custom = load_trust_store().skill
    if matches_any_prefix(source_key, custom):
        return "custom"
    return "untrusted"


def source_key_for_url(url: str) -> str:
    """Normalise a skill install URL into a trust-prefix shape.

    GitHub URLs collapse to ``github.com/<owner>/<repo>``, raw
    GitHub URLs collapse to the same so the two forms share one
    trust prefix. Every other URL strips the scheme so users can
    write prefixes like ``raw.example.com/*`` without thinking
    about ``http`` vs ``https``.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    parts = [p for p in path.split("/") if p]

    if host == "github.com" and len(parts) >= 2:
        owner = parts[0]
        repo = parts[1].removesuffix(".git")
        return f"github.com/{owner}/{repo}"
    if host == "raw.githubusercontent.com" and len(parts) >= 2:
        return f"github.com/{parts[0]}/{parts[1]}"
    if host:
        key = host + path
        return key.rstrip("/")
    return url


def detect_risk_factors(skill_path: Path) -> list[str]:
    """Return a list of human-readable risk-factor strings.

    Accepts either a directory skill (with ``SKILL.md``) or a
    single-file skill (``<name>.md``). Returns ``[]`` when nothing
    stands out — the common case for a well-formed, minimal skill.
    """
    if skill_path.is_file() and skill_path.suffix == ".md":
        return _risk_factors_for_file(skill_path)
    if skill_path.is_dir():
        return _risk_factors_for_dir(skill_path)
    return []


def _risk_factors_for_dir(d: Path) -> list[str]:
    risks: list[str] = []
    skill_md = d / "SKILL.md"

    scripts = d / "scripts"
    if scripts.is_dir() and any(scripts.iterdir()):
        risks.append("bundled scripts/ directory — executable content")

    if skill_md.is_file():
        body = skill_md.read_text(encoding="utf-8", errors="replace")
        if skill_md.stat().st_size > _MAX_SKILL_MD_BYTES:
            risks.append(
                f"SKILL.md exceeds {_MAX_SKILL_MD_BYTES // 1024} KB "
                f"({skill_md.stat().st_size // 1024} KB)"
            )
        if _has_risky_allowed_tools(body):
            risks.append(
                "allowed-tools grants wide shell / write / edit scope"
            )

    assets = d / "assets"
    if assets.is_dir():
        total = _dir_size(assets)
        if total > _MAX_ASSETS_BYTES:
            risks.append(
                f"assets/ exceeds {_MAX_ASSETS_BYTES // (1024 * 1024)} MB "
                f"({total // (1024 * 1024)} MB)"
            )

    return risks


def _risk_factors_for_file(p: Path) -> list[str]:
    risks: list[str] = []
    body = p.read_text(encoding="utf-8", errors="replace")
    if p.stat().st_size > _MAX_SKILL_MD_BYTES:
        risks.append(
            f"SKILL.md exceeds {_MAX_SKILL_MD_BYTES // 1024} KB "
            f"({p.stat().st_size // 1024} KB)"
        )
    if _has_risky_allowed_tools(body):
        risks.append(
            "allowed-tools grants wide shell / write / edit scope"
        )
    return risks


def _has_risky_allowed_tools(body: str) -> bool:
    return _RISKY_TOOL_RE.search(body) is not None


def _dir_size(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total
