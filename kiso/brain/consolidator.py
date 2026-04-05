"""Knowledge consolidator role logic."""

from __future__ import annotations

import logging

import aiosqlite

from kiso.config import Config
from kiso.store import delete_facts, get_all_entities, get_facts, update_fact_content

from .common import (
    _build_messages,
    _build_strict_schema,
    _load_system_prompt,
    _retry_llm_with_validation,
)

log = logging.getLogger("kiso.brain")

_IMPORTED_NAMES = set(globals())

class ConsolidatorError(Exception):
    """Consolidator validation or generation failure."""


CONSOLIDATOR_SCHEMA: dict = _build_strict_schema("consolidator", {
    "delete": {"type": "array", "items": {"type": "integer"}},
    "update": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "content": {"type": "string"},
        },
        "required": ["id", "content"],
        "additionalProperties": False,
    }},
    "keep": {"type": "array", "items": {"type": "integer"}},
}, ["delete", "update", "keep"])


def build_consolidator_messages(facts_by_entity: dict[str, list[dict]]) -> list[dict]:
    """Build the message list for the consolidator LLM call.

    *facts_by_entity* maps entity name (or "(no entity)") to a list of
    fact dicts, each with at least ``id`` and ``content``.
    """
    system_prompt = _load_system_prompt("consolidator")
    parts: list[str] = []
    for entity_name, facts in sorted(facts_by_entity.items()):
        lines = "\n".join(f"  [{f['id']}] {f['content']}" for f in facts)
        parts.append(f"### {entity_name}\n{lines}")
    user_content = "## Stored Facts\n\n" + "\n\n".join(parts)
    return _build_messages(system_prompt, user_content)


def validate_consolidator(result: dict, expected_ids: set[int]) -> list[str]:
    """Validate consolidator result. Returns list of error strings (empty = valid)."""
    errors: list[str] = []
    delete_ids = set(result.get("delete", []))
    update_ids = {item["id"] for item in result.get("update", [])}
    keep_ids = set(result.get("keep", []))

    all_mentioned = delete_ids | update_ids | keep_ids
    # Check for overlap between categories
    if delete_ids & update_ids:
        errors.append(f"IDs in both delete and update: {sorted(delete_ids & update_ids)}")
    if delete_ids & keep_ids:
        errors.append(f"IDs in both delete and keep: {sorted(delete_ids & keep_ids)}")
    if update_ids & keep_ids:
        errors.append(f"IDs in both update and keep: {sorted(update_ids & keep_ids)}")

    missing = expected_ids - all_mentioned
    if missing:
        errors.append(f"Missing fact IDs: {sorted(missing)}")
    extra = all_mentioned - expected_ids
    if extra:
        errors.append(f"Unknown fact IDs: {sorted(extra)}")

    # Check update items have non-empty content
    for item in result.get("update", []):
        if not item.get("content", "").strip():
            errors.append(f"Update for fact {item['id']} has empty content")

    return errors


def _group_facts_by_entity(
    facts: list[dict], entities: list[dict],
) -> dict[str, list[dict]]:
    """Group facts by entity name. Facts without entity go under '(no entity)'."""
    entity_map = {e["id"]: e["name"] for e in entities}
    grouped: dict[str, list[dict]] = {}
    for f in facts:
        name = entity_map.get(f.get("entity_id")) or "(no entity)"
        grouped.setdefault(name, []).append(f)
    return grouped


async def run_consolidator(
    config: Config, db: aiosqlite.Connection, session: str = "",
) -> dict:
    """Run the consolidator on all stored facts.

    Returns dict with keys: delete, update, keep.
    Raises ConsolidatorError if all retries exhausted.
    """
    all_facts = await get_facts(db, is_admin=True)
    if not all_facts:
        return {"delete": [], "update": [], "keep": []}

    entities = await get_all_entities(db)
    facts_by_entity = _group_facts_by_entity(all_facts, entities)
    messages = build_consolidator_messages(facts_by_entity)
    expected_ids = {f["id"] for f in all_facts}

    result = await _retry_llm_with_validation(
        config, "consolidator", messages, CONSOLIDATOR_SCHEMA,
        lambda r: validate_consolidator(r, expected_ids),
        ConsolidatorError, "Consolidator",
        session=session,
    )
    log.info(
        "Consolidator: delete=%d update=%d keep=%d",
        len(result["delete"]), len(result["update"]), len(result["keep"]),
    )
    return result


async def apply_consolidation_result(db: aiosqlite.Connection, result: dict) -> None:
    """Apply consolidator result: delete and update facts."""
    # Deletions
    to_delete = result.get("delete", [])
    if to_delete:
        await delete_facts(db, to_delete)

    # Updates
    for item in result.get("update", []):
        content = item.get("content", "").strip()
        if content:
            await update_fact_content(db, item["id"], content)


__brain_exports__ = [
    name
    for name in globals()
    if name not in _IMPORTED_NAMES and name not in {"__brain_exports__", "_IMPORTED_NAMES"}
]

del _IMPORTED_NAMES
