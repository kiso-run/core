"""Curator-specific evaluation logic."""

from __future__ import annotations

import logging

from kiso.config import Config

from .common import (
    CURATOR_VERDICT_ASK,
    CURATOR_VERDICT_PROMOTE,
    MemoryPack,
    _ENTITY_KINDS,
    _MIN_PROMOTED_FACT_LEN,
    _VALID_FACT_CATEGORIES,
    _add_section,
    _build_strict_schema,
    _build_messages_from_sections,
    _build_curator_memory_pack,
    _load_modular_prompt,
    _require_memory_pack_role,
    _retry_llm_with_validation,
)

log = logging.getLogger("kiso.brain")

_IMPORTED_NAMES = set(globals())

CURATOR_SCHEMA: dict = _build_strict_schema("curator", {
    "evaluations": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "learning_id": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["promote", "ask", "discard"]},
            "fact": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "category": {"anyOf": [{"type": "string", "enum": ["project", "user", "tool", "general", "behavior"]}, {"type": "null"}]},
            "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "reason": {"type": "string"},
            "tags": {"anyOf": [
                {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                {"type": "null"},
            ]},
            "entity_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "entity_kind": {"anyOf": [
                {"type": "string", "enum": sorted(_ENTITY_KINDS)},
                {"type": "null"},
            ]},
        },
        "required": ["learning_id", "verdict", "fact", "category", "question", "reason", "tags", "entity_name", "entity_kind"],
        "additionalProperties": False,
    }},
}, ["evaluations"])

class CuratorError(Exception):
    """Curator validation or generation failure."""


class SummarizerError(Exception):
    """Summarizer generation failure."""


_MIN_PROMOTED_FACT_LEN = 10


def validate_curator(result: dict, expected_count: int | None = None) -> list[str]:
    """Validate curator result semantics. Returns list of error strings.

    *expected_count* is the number of input learnings. The curator may return
    **fewer** evaluations (consolidation) but never **more** than expected.
    """
    errors: list[str] = []
    evals = result.get("evaluations", [])
    if expected_count is not None and len(evals) > expected_count:
        errors.append(f"Expected at most {expected_count} evaluations, got {len(evals)}")
    if expected_count is not None and expected_count > 0 and len(evals) == 0:
        errors.append("Expected at least 1 evaluation but got 0 — every learning must be evaluated")
    for i, ev in enumerate(evals, 1):
        verdict = ev.get("verdict")
        if not ev.get("reason"):
            errors.append(f"Evaluation {i}: reason is required")
        if verdict == CURATOR_VERDICT_PROMOTE:
            fact = ev.get("fact")
            if not fact:
                errors.append(f"Evaluation {i}: promote verdict requires a non-empty fact")
            elif len(fact) < _MIN_PROMOTED_FACT_LEN:
                errors.append(f"Evaluation {i}: promoted fact too short ({len(fact)} chars, min {_MIN_PROMOTED_FACT_LEN})")
        if verdict == CURATOR_VERDICT_PROMOTE and ev.get("category") is not None:
            if ev["category"] not in _VALID_FACT_CATEGORIES:
                errors.append(f"Evaluation {i}: category must be one of {sorted(_VALID_FACT_CATEGORIES)}")
        if verdict == CURATOR_VERDICT_ASK and not ev.get("question"):
            errors.append(f"Evaluation {i}: ask verdict requires a non-empty question")
        # entity required for promote
        if verdict == CURATOR_VERDICT_PROMOTE:
            if not ev.get("entity_name"):
                errors.append(f"Evaluation {i}: promoted fact must have entity_name")
            kind = ev.get("entity_kind")
            if not kind or kind not in _ENTITY_KINDS:
                errors.append(f"Evaluation {i}: promoted fact must have valid entity_kind")
    return errors


def _select_curator_modules() -> list[str]:
    """Select prompt modules for the curator.

    Always includes ``entity_assignment`` (needed for any promote) and
    ``tag_reuse`` (tag formatting/semantic-retrieval guidance).  When no
    existing tags are available the "check existing tags first" instruction
    is a harmless no-op; the formatting rules ("lowercase, hyphenated")
    are always valuable and ensure promoted facts get well-formed tags.
    """
    return ["entity_assignment", "tag_reuse"]


def build_curator_messages(
    learnings: list[dict],
    available_tags: list[str] | None = None,
    available_entities: list[dict] | None = None,
    existing_facts: list[dict] | None = None,
    memory_pack: MemoryPack | None = None,
) -> list[dict]:
    """Build the message list for the curator LLM call."""
    if memory_pack is not None:
        _require_memory_pack_role(memory_pack, "curator")
        available_tags = memory_pack.available_tags or available_tags
        available_entities = memory_pack.available_entities or available_entities
    modules = _select_curator_modules()
    system_prompt = _load_modular_prompt("curator", modules)
    items = "\n".join(
        f"{i}. [id={l['id']}] {l['content']}"
        for i, l in enumerate(learnings, 1)
    )
    parts = [f"## Learnings\n{items}"]
    _add_section(parts, "Existing Tags", ", ".join(available_tags) if available_tags else "")
    if available_entities:
        entity_lines = "\n".join(f"{e['name']} ({e['kind']})" for e in available_entities)
        parts.append(f"## Existing Entities\n{entity_lines}")
    if existing_facts:
        fact_lines = "\n".join(
            f"[entity: {f.get('entity_name', '?')}] {f['content']}" for f in existing_facts
        )
        parts.append(f"## Existing Facts (already in knowledge base)\n{fact_lines}")
    return _build_messages_from_sections(system_prompt, parts)


async def run_curator(
    config: Config,
    learnings: list[dict],
    session: str = "",
    available_tags: list[str] | None = None,
    available_entities: list[dict] | None = None,
    existing_facts: list[dict] | None = None,
) -> dict:
    """Run the curator on pending learnings.

    Returns dict with key "evaluations".
    Raises CuratorError if all retries exhausted.
    """
    memory_pack = _build_curator_memory_pack(
        available_tags=available_tags,
        available_entities=available_entities,
    )
    messages = build_curator_messages(
        learnings, available_tags=available_tags,
        available_entities=available_entities,
        existing_facts=existing_facts,
        memory_pack=memory_pack,
    )
    expected = len(learnings)
    result = await _retry_llm_with_validation(
        config, "curator", messages, CURATOR_SCHEMA,
        lambda r: validate_curator(r, expected_count=expected),
        CuratorError, "Curator",
        session=session,
    )
    log.info("Curator: %d evaluations", len(result["evaluations"]))
    return result


__brain_exports__ = [
    name
    for name in globals()
    if name not in _IMPORTED_NAMES and name not in {"__brain_exports__", "_IMPORTED_NAMES"}
]

del _IMPORTED_NAMES


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------
