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
)


def metadata_for_briefer(skill: Skill) -> dict[str, Any]:
    """Return the subset of skill metadata visible to the briefer.

    Intentionally excludes the body and bundled-file paths: the briefer
    reasons about *what* to select, not *how* to execute.
    """
    out: dict[str, Any] = {
        "name": skill.name,
        "description": skill.description,
    }
    if skill.when_to_use:
        out["when_to_use"] = skill.when_to_use
    if skill.audiences:
        out["audiences"] = list(skill.audiences)
    return out


def _audience_allows(skill: Skill, role: str) -> bool:
    if skill.audiences is None:
        return True
    return role in skill.audiences


def instructions_for_planner(skill: Skill) -> str:
    if not _audience_allows(skill, "planner"):
        return ""
    planner = skill.role_sections.get("planner")
    if planner:
        return planner
    # Fallback: body with no role sections is planner-only.
    if not skill.role_sections and skill.body:
        return skill.body
    return ""


def instructions_for_worker(skill: Skill) -> str:
    if not _audience_allows(skill, "worker"):
        return ""
    return skill.role_sections.get("worker", "")


def instructions_for_reviewer(skill: Skill) -> str:
    if not _audience_allows(skill, "reviewer"):
        return ""
    return skill.role_sections.get("reviewer", "")


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
        hints = s.activation_hints or None
        if hints is None:
            kept.append(s)
            continue
        applies = hints.get("applies_to") or []
        excludes = hints.get("excludes") or []
        if applies and not any(
            _selector_matches(message_norm, sel) for sel in applies
        ):
            continue
        if any(_selector_matches(message_norm, sel) for sel in excludes):
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
