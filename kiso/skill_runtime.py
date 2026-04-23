"""Role-scoped projection of skills into runtime prompts.

The :mod:`kiso.skill_loader` returns :class:`~kiso.skill_loader.Skill`
objects with all standard Agent Skills frontmatter plus Kiso extension
fields. This module decides *what each role sees*:

- :func:`metadata_for_briefer` — public metadata only; never the body.
- :func:`instructions_for_planner` — the ``## Planner`` section, or
  the whole body as planner-only fallback when role sections are
  absent.
- :func:`instructions_for_worker` / ``_reviewer`` / ``_messenger`` —
  only the matching ``## Role`` section; no body fallback (bodies
  without role sections are planner-only by convention).
- All role projections honor the ``audiences`` frontmatter field: if
  present and the role is not listed, the projection is an empty
  string.

Plus :func:`filter_by_activation_hints`: deterministic pre-filter
applied before the briefer runs, identical semantics to the recipe
``applies_to`` / ``excludes`` word-aware selectors.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from kiso.skill_loader import Skill

__all__ = (
    "metadata_for_briefer",
    "instructions_for_planner",
    "instructions_for_worker",
    "instructions_for_reviewer",
    "instructions_for_messenger",
    "filter_by_activation_hints",
    "log_activation_miss",
)


log = logging.getLogger(__name__)


def log_activation_miss(
    *,
    skill_name: str,
    message: str,
    applies_to: list[str],
    excludes: list[str],
) -> None:
    """Emit a DEBUG log explaining why a skill was filtered out.

    No-op unless ``KISO_DEBUG`` is set in the process environment —
    this must not spam normal operation. Intended for a user trying
    to figure out why an installed skill never activates on a given
    message.
    """
    if not os.environ.get("KISO_DEBUG"):
        return
    msg_preview = message if len(message) <= 80 else message[:80] + "…"
    log.debug(
        "skill %s filtered: applies_to=%s no match in %r; excludes=%s",
        skill_name,
        applies_to,
        msg_preview,
        excludes,
    )


def _attr(obj, name, default=None):
    """Return attribute or dict key; tolerates mocks passed as plain dicts."""
    if hasattr(obj, name):
        return getattr(obj, name, default)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def metadata_for_briefer(skill: Skill) -> dict[str, Any]:
    """Return the subset of skill metadata visible to the briefer.

    Intentionally excludes the body and bundled-file paths: the briefer
    reasons about *what* to select, not *how* to execute.
    """
    out: dict[str, Any] = {
        "name": _attr(skill, "name", ""),
        "description": _attr(skill, "description", "") or _attr(skill, "summary", ""),
    }
    when_to_use = _attr(skill, "when_to_use")
    if when_to_use:
        out["when_to_use"] = when_to_use
    audiences = _attr(skill, "audiences")
    if audiences:
        out["audiences"] = list(audiences)
    return out


def _audience_allows(skill: Skill, role: str) -> bool:
    audiences = _attr(skill, "audiences")
    if audiences is None:
        return True
    return role in audiences


def instructions_for_planner(skill: Skill) -> str:
    if not _audience_allows(skill, "planner"):
        return ""
    role_sections = _attr(skill, "role_sections", {}) or {}
    planner = role_sections.get("planner")
    if planner:
        return planner
    body = _attr(skill, "body", "")
    if not role_sections and body:
        return body
    return ""


def instructions_for_worker(skill: Skill) -> str:
    if not _audience_allows(skill, "worker"):
        return ""
    role_sections = _attr(skill, "role_sections", {}) or {}
    return role_sections.get("worker", "")


def instructions_for_reviewer(skill: Skill) -> str:
    if not _audience_allows(skill, "reviewer"):
        return ""
    role_sections = _attr(skill, "role_sections", {}) or {}
    return role_sections.get("reviewer", "")


def instructions_for_messenger(skill: Skill) -> str:
    if not _audience_allows(skill, "messenger"):
        return ""
    return skill.role_sections.get("messenger", "")


# ---------------------------------------------------------------------------
# Activation hints pre-filter
# ---------------------------------------------------------------------------


def filter_by_activation_hints(
    skills: list[Skill], message: str, *, is_replan: bool = False
) -> list[Skill]:
    """Deterministically drop skills that don't match the message.

    Semantics match the legacy recipe ``applies_to``/``excludes``
    selectors:

    - ``applies_to`` non-empty → at least one selector must match, OR
      the skill is dropped.
    - ``excludes`` non-empty → any selector match drops the skill.
    - Single-token selectors match word-bounded; multi-word selectors
      match as case-insensitive substrings on a space-normalised copy.

    An empty message disables filtering (all skills kept).
    ``is_replan=True`` bypasses the filter entirely — during replan the
    original user message may not mention a skill that the planner now
    needs (e.g. after an install).
    """
    if is_replan:
        return list(skills)
    if not skills or not message:
        return list(skills)

    message_norm = " ".join(message.lower().split())
    kept: list[Skill] = []
    for s in skills:
        hints = getattr(s, "activation_hints", None) or None
        if hints is None:
            kept.append(s)
            continue
        applies = hints.get("applies_to") or []
        excludes = hints.get("excludes") or []
        if applies and not any(
            _selector_matches(message_norm, sel) for sel in applies
        ):
            log_activation_miss(
                skill_name=getattr(s, "name", "<unknown>"),
                message=message,
                applies_to=list(applies),
                excludes=list(excludes),
            )
            continue
        if any(_selector_matches(message_norm, sel) for sel in excludes):
            log_activation_miss(
                skill_name=getattr(s, "name", "<unknown>"),
                message=message,
                applies_to=list(applies),
                excludes=list(excludes),
            )
            continue
        kept.append(s)
    return kept


def _selector_matches(message_norm: str, selector: str) -> bool:
    sel = " ".join(selector.lower().split())
    if not sel:
        return False
    if " " in sel:
        return sel in message_norm
    return re.search(rf"\b{re.escape(sel)}\b", message_norm) is not None
