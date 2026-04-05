"""Prompt loading and prompt-section rendering helpers for `kiso.brain`."""

from __future__ import annotations

import re
from pathlib import Path

from kiso.config import KISO_DIR

_ROLES_DIR = Path(__file__).resolve().parent.parent / "roles"
_prompt_cache: dict[str, str] = {}
_MODULE_MARKER_RE = re.compile(r"<!--\s*MODULE:\s*(\w+)\s*-->")
_ANSWER_IN_LANG_RE = re.compile(r"^Answer in (\w[\w\s]*)\.")


def _load_system_prompt(role: str) -> str:
    """Load system prompt: user override first, then package default."""
    if role in _prompt_cache:
        return _prompt_cache[role]
    user_path = KISO_DIR / "roles" / f"{role}.md"
    if user_path.exists():
        text = user_path.read_text()
        _prompt_cache[role] = text
        return text
    pkg_path = _ROLES_DIR / f"{role}.md"
    if pkg_path.exists():
        text = pkg_path.read_text()
        _prompt_cache[role] = text
        return text
    raise FileNotFoundError(f"No prompt found for role '{role}'")


def invalidate_prompt_cache() -> None:
    """Clear the in-process system-prompt cache."""
    _prompt_cache.clear()


def _load_modular_prompt(role: str, modules: list[str]) -> str:
    """Load a role prompt, returning only core + selected modules."""
    full_text = _load_system_prompt(role)
    return _render_modular_prompt_text(full_text, modules)


def _render_modular_prompt_text(full_text: str, modules: list[str]) -> str:
    """Render a modular prompt from already-loaded prompt text."""
    parts = _MODULE_MARKER_RE.split(full_text)
    if len(parts) <= 1:
        return full_text

    wanted = {"core"} | set(modules)
    sections: list[str] = []
    for i in range(1, len(parts), 2):
        name = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if name in wanted:
            sections.append(body)
    stripped = [s.strip() for s in sections]
    return "\n".join(s for s in stripped if s)


def _build_messages(system_prompt: str, user_content: str) -> list[dict]:
    """Assemble the canonical [system, user] message pair used by all LLM roles."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _build_messages_from_sections(system_prompt: str, parts: list[str]) -> list[dict]:
    """Assemble the canonical message pair from pre-rendered context sections."""
    return _build_messages(system_prompt, "\n\n".join(parts))


def _add_section(parts: list[str], name: str, content: str) -> None:
    """Append a ``## {name}`` section to *parts* if *content* is non-empty."""
    if content:
        parts.append(f"## {name}\n{content}")


def _add_context_section(
    parts: list[str], context_sections: dict[str, str], key: str, title: str,
) -> None:
    """Render a named prompt section directly from structured context state."""
    _add_section(parts, title, context_sections.get(key, ""))


__brain_exports__ = [
    "_ANSWER_IN_LANG_RE",
    "_ROLES_DIR",
    "_add_context_section",
    "_add_section",
    "_build_messages",
    "_build_messages_from_sections",
    "_load_modular_prompt",
    "_load_system_prompt",
    "_prompt_cache",
    "invalidate_prompt_cache",
]
