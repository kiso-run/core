"""Discover and parse standard Agent Skills from ``~/.kiso/skills/``.

Agent Skills (``agentskills.io``) are the instruction primitive for
kiso's planner / worker / reviewer / messenger roles. Each skill is
either:

- a directory ``<name>/`` containing ``SKILL.md`` plus optional
  ``scripts/``, ``references/``, ``assets/`` — the canonical shape
- a single file ``<name>.md`` — lightweight fallback

``SKILL.md`` is Markdown with YAML frontmatter. Standard fields
(``name``, ``description``, ``license``, ``compatibility``,
``metadata``, ``allowed-tools``) are preserved verbatim. Kiso extension
fields (``when_to_use``, ``audiences``, ``activation_hints``,
``version``) enable richer runtime behavior.

The body may contain role-scoped headings ``## Planner``,
``## Worker``, ``## Reviewer``, ``## Messenger`` — extracted into
``Skill.role_sections`` for projection by the skill runtime
(``kiso/skill_runtime.py``). Bodies without role headings default to
planner-only guidance.

Skill names follow the Agent Skills standard: lowercase letters, digits,
and hyphens only; 1-64 characters; must start with a letter or digit.
Non-conforming names are logged and skipped.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

_SKILLS_TTL = 30  # seconds — parity with recipe_loader

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ROLE_HEADING_RE = re.compile(
    r"^##\s+(planner|worker|reviewer|messenger)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

_VALID_ROLES = ("planner", "worker", "reviewer", "messenger")


@dataclass(frozen=True)
class Skill:
    """Parsed Agent Skill.

    Standard fields follow the ``agentskills.io`` spec; Kiso extension
    fields are optional and additive.
    """

    # Standard
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] | None = None
    allowed_tools: str | None = None
    # Kiso extensions
    when_to_use: str | None = None
    audiences: list[str] | None = None
    activation_hints: dict[str, list[str]] | None = None
    version: str | None = None
    # Content
    body: str = ""
    role_sections: dict[str, str] = field(default_factory=dict)
    # Paths
    path: Path | None = None  # path to SKILL.md or single-file skill
    bundled_root: Path | None = None  # directory root for dir skills


_cache: dict[Path, tuple[float, list[Skill]]] = {}


def discover_skills(skills_dir: Path | None = None) -> list[Skill]:
    """Discover installed skills.

    Scans directory skills (``<dir>/SKILL.md``) and single-file skills
    (``<dir>/<name>.md``) under *skills_dir*. Returns a sorted list of
    :class:`Skill` instances. Unparseable or invalid skills are logged
    and skipped.

    Results cached with TTL to avoid repeated filesystem scans.
    """
    d = skills_dir or (KISO_DIR / "skills")
    now = time.monotonic()
    cached = _cache.get(d)
    if cached and (now - cached[0]) < _SKILLS_TTL:
        return cached[1]

    if not d.is_dir():
        _cache[d] = (now, [])
        return []

    skills: list[Skill] = []
    # Directory skills take precedence over same-named single-file skills.
    seen_names: set[str] = set()
    for child in sorted(d.iterdir()):
        if child.is_dir():
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                parsed = parse_skill_file(skill_md, bundled_root=child)
                if parsed and parsed.name not in seen_names:
                    skills.append(parsed)
                    seen_names.add(parsed.name)
        elif child.suffix == ".md":
            parsed = parse_skill_file(child)
            if parsed and parsed.name not in seen_names:
                skills.append(parsed)
                seen_names.add(parsed.name)

    _cache[d] = (now, skills)
    log.debug("Discovered %d skills in %s", len(skills), d)
    return skills


def invalidate_skills_cache() -> None:
    """Clear the skills cache."""
    _cache.clear()


def parse_skill_file(
    path: Path, *, bundled_root: Path | None = None
) -> Skill | None:
    """Parse a single SKILL.md file.

    Returns a :class:`Skill` instance on success, or None if the file
    has malformed frontmatter, missing required fields, or an invalid
    skill name.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("Cannot read skill file: %s", path)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        log.warning("Skill file missing or malformed frontmatter: %s", path)
        return None

    fm_raw, body = match.group(1), match.group(2).strip()
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as exc:
        log.warning("Skill file has invalid YAML frontmatter %s: %s", path, exc)
        return None
    if not isinstance(fm, dict):
        log.warning("Skill frontmatter must be a mapping: %s", path)
        return None

    name = fm.get("name")
    description = fm.get("description")
    if not isinstance(name, str) or not name:
        log.warning("Skill missing required 'name': %s", path)
        return None
    if not isinstance(description, str) or not description:
        log.warning("Skill missing required 'description': %s", path)
        return None
    if not _NAME_RE.match(name):
        log.warning(
            "Skill name %r violates Agent Skills naming rule (lowercase,"
            " hyphens, digits, 1-64 chars, no leading hyphen): %s",
            name,
            path,
        )
        return None

    role_sections = _extract_role_sections(body)

    return Skill(
        name=name,
        description=description,
        license=_opt_str(fm.get("license")),
        compatibility=_opt_str(fm.get("compatibility")),
        metadata=_opt_dict(fm.get("metadata")),
        allowed_tools=_opt_str(fm.get("allowed-tools")),
        when_to_use=_opt_str(fm.get("when_to_use")),
        audiences=_opt_str_list(fm.get("audiences")),
        activation_hints=_parse_activation_hints(fm.get("activation_hints")),
        version=_opt_str(fm.get("version")),
        body=body,
        role_sections=role_sections,
        path=path,
        bundled_root=bundled_root,
    )


def _extract_role_sections(body: str) -> dict[str, str]:
    """Split body on ``## <Role>`` headings into per-role section text.

    Returns a dict keyed by lowercased role name. Content before the
    first role heading is ignored (it belongs to the generic body).
    Only the four known roles are captured.
    """
    matches = list(_ROLE_HEADING_RE.finditer(body))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        role = m.group(1).lower()
        if role not in _VALID_ROLES:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        if content:
            sections[role] = content
    return sections


# ---------------------------------------------------------------------------
# Frontmatter field coercions — keep permissive and type-safe.
# ---------------------------------------------------------------------------


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _opt_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    return None


def _opt_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return None


def _parse_activation_hints(value: Any) -> dict[str, list[str]] | None:
    """Normalise activation_hints into ``{applies_to, excludes}``.

    Accepts either the structured form ``{applies_to: [...], excludes:
    [...]}`` or a bare list (interpreted as ``applies_to``).
    """
    if value is None:
        return None
    if isinstance(value, list):
        return {"applies_to": [str(v) for v in value if v], "excludes": []}
    if isinstance(value, dict):
        applies = value.get("applies_to")
        excludes = value.get("excludes")
        applies_list = [str(v) for v in applies] if isinstance(applies, list) else []
        excludes_list = (
            [str(v) for v in excludes] if isinstance(excludes, list) else []
        )
        if applies_list or excludes_list:
            return {"applies_to": applies_list, "excludes": excludes_list}
    return None
