"""Prompt loading and prompt-section rendering helpers for `kiso.brain`."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from kiso.config import KISO_DIR

log = logging.getLogger(__name__)

# _ROLES_DIR is the install-time source of bundled roles. The eager
# copy in kiso.main._init_kiso_dirs() seeds ~/.kiso/roles/ at boot;
# the lazy self-heal in _load_system_prompt() handles missing or
# empty files at first access (runtime corruption / fresh test
# KISO_DIR). After self-heal the user dir is the runtime source of
# truth — the bundle is the factory seed, not an alternate read
# path.
_ROLES_DIR = Path(__file__).resolve().parent.parent / "roles"
_prompt_cache: dict[str, str] = {}
_MODULE_MARKER_RE = re.compile(r"<!--\s*MODULE:\s*(\w+)\s*-->")
_ANSWER_IN_LANG_RE = re.compile(r"^Answer in (\w[\w\s]*)\.")


def _self_heal_role(user_path: Path, pkg_path: Path, role: str) -> None:
    """Atomically copy *pkg_path* → *user_path* and log a warning.

    Used by :func:`_load_system_prompt` when the user role file is
    missing or empty. The write goes through a temp file + rename
    so concurrent self-heal from two workers cannot leave a
    half-written file on disk.

    Logs a WARNING containing the role name, the user dir path,
    and a notice that any prior customization is lost. Operators
    should see this in their logs after a runtime corruption or
    after running kiso against a fresh KISO_DIR.
    """
    user_path.parent.mkdir(parents=True, exist_ok=True)
    bundled_text = pkg_path.read_text(encoding="utf-8")
    tmp_path = user_path.with_suffix(user_path.suffix + ".tmp")
    tmp_path.write_text(bundled_text, encoding="utf-8")
    tmp_path.replace(user_path)
    log.warning(
        "Self-healed role '%s' from bundled default into %s. "
        "Any previous customization is lost; restore from backup if needed.",
        role, user_path,
    )


def _load_system_prompt(role: str) -> str:
    """Load system prompt for *role* from the user roles directory.

    Reads from ``KISO_DIR / "roles" / "{role}.md"``. The user dir
    is the runtime source of truth.

    **Lazy self-heal**: when the user file is missing or empty
    (deleted, never created, or corrupted to zero bytes), the
    loader copies the bundled default into the user dir
    atomically, logs a WARNING, and reads the seeded file. This
    mirrors the eager seeding done by
    :func:`kiso.main._init_kiso_dirs` at server startup but
    catches files that go missing AFTER startup (runtime
    corruption, fresh test KISO_DIRs, container restarts on
    ephemeral volumes).

    Raises :class:`FileNotFoundError` only when both the user
    file and the bundled default are missing — i.e., the kiso
    installation itself is corrupted and reinstalling is the
    only fix.
    """
    if role in _prompt_cache:
        return _prompt_cache[role]
    user_path = KISO_DIR / "roles" / f"{role}.md"
    if not user_path.exists() or user_path.stat().st_size == 0:
        pkg_path = _ROLES_DIR / f"{role}.md"
        if not pkg_path.exists():
            raise FileNotFoundError(
                f"Role file '{role}' missing from both user dir "
                f"({user_path}) and package bundle ({pkg_path}). "
                f"The kiso installation may be corrupted; reinstall."
            )
        _self_heal_role(user_path, pkg_path, role)
    text = user_path.read_text()
    _prompt_cache[role] = text
    return text


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
