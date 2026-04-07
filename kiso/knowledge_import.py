"""Parse markdown files into atomic knowledge facts for import."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ImportedFact:
    content: str
    category: str = "general"
    entity_name: str | None = None
    entity_kind: str | None = None
    tags: list[str] = field(default_factory=list)


# Match `## Entity: name (kind)` headings
_ENTITY_HEADING_RE = re.compile(
    r"^##\s+Entity:\s*(.+?)\s*\((\w+)\)\s*$", re.IGNORECASE
)

# Match `## Behaviors` heading (case-insensitive)
_BEHAVIORS_HEADING_RE = re.compile(r"^##\s+Behaviors?\s*$", re.IGNORECASE)

# Match any `## ...` heading (to detect section boundaries)
_HEADING_RE = re.compile(r"^##\s+")

# Match inline tags: #tag at end of line
_INLINE_TAGS_RE = re.compile(r"\s+#(\w[\w-]*)")

# Match bullet points: `- text` or `* text`
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")


def parse_knowledge_markdown(text: str, default_category: str = "general") -> list[ImportedFact]:
    """Parse a markdown file into a list of ImportedFact objects.

    Supported structure:
    - ``## Entity: name (kind)`` → sets entity context for subsequent facts
    - ``- fact text #tag1 #tag2`` → bullet point becomes a fact with inline tags
    - Plain paragraphs → split into sentences, each becomes a fact
    - ``## Behaviors`` → subsequent facts get category="behavior"
    """
    facts: list[ImportedFact] = []
    current_entity: str | None = None
    current_kind: str | None = None
    current_category: str = default_category

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Check for entity heading
        entity_m = _ENTITY_HEADING_RE.match(stripped)
        if entity_m:
            current_entity = entity_m.group(1).strip()
            current_kind = entity_m.group(2).strip()
            current_category = default_category
            continue

        # Check for behaviors heading
        if _BEHAVIORS_HEADING_RE.match(stripped):
            current_entity = None
            current_kind = None
            current_category = "behavior"
            continue

        # Check for other headings (reset entity context)
        if _HEADING_RE.match(stripped):
            current_entity = None
            current_kind = None
            current_category = default_category
            continue

        # Extract inline tags
        tags: list[str] = []
        tag_matches = _INLINE_TAGS_RE.findall(stripped)
        if tag_matches:
            tags = [t.lower() for t in tag_matches]
            # Remove tags from content
            content = _INLINE_TAGS_RE.sub("", stripped).strip()
        else:
            content = stripped

        # Check for bullet point
        bullet_m = _BULLET_RE.match(content)
        if bullet_m:
            content = bullet_m.group(1).strip()

        if not content or len(content) < 5:
            continue

        facts.append(ImportedFact(
            content=content,
            category=current_category,
            entity_name=current_entity,
            entity_kind=current_kind,
            tags=tags,
        ))

    return facts
