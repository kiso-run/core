"""Invariant: no doc under ``docs/`` or ``kiso/reference/`` still
teaches the reader how to use the retired wrapper, recipe, or
``search`` task-type subsystems.

Regressions here mean new documentation is advertising systems that
the runtime no longer supports. Historical references in past tense
are allowed; active usage instructions (install commands, path
conventions) are not.
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _doc_files() -> list[Path]:
    out: list[Path] = []
    for folder in (ROOT / "docs", ROOT / "kiso" / "reference"):
        if not folder.is_dir():
            continue
        out.extend(p for p in folder.rglob("*.md"))
    return out


class TestNoRetiredUsageStrings:
    """No doc file advertises the retired CLI verbs or directory
    conventions as current usage."""

    FORBIDDEN = (
        "kiso wrapper install",
        "kiso wrapper create",
        "kiso wrapper remove",
        "kiso wrapper list",
        "kiso recipe install",
        "kiso recipe remove",
        "kiso recipe list",
        "~/.kiso/wrappers/",
        "~/.kiso/recipes/",
    )

    @pytest.mark.parametrize("phrase", FORBIDDEN)
    def test_phrase_absent_across_docs(self, phrase: str):
        offenders = [
            p.relative_to(ROOT)
            for p in _doc_files()
            if phrase in p.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            f"retired usage string '{phrase}' still appears in: "
            f"{', '.join(str(o) for o in offenders)}"
        )


class TestExtensibilityDocCurrent:

    def test_extensibility_doc_has_no_wrapper_section(self):
        doc = ROOT / "docs" / "extensibility.md"
        if not doc.is_file():
            pytest.skip("docs/extensibility.md not present")
        text = doc.read_text(encoding="utf-8").lower()
        # The v0.10 extensibility surface is: skills, MCP servers,
        # connectors. Wrappers and recipes are retired.
        assert "skill" in text
        assert "mcp" in text
        # The doc must not open with a Wrapper section — the v0.9
        # version was wrapper-first.
        assert "### 1. wrapper" not in text
        assert "### 3. recipe" not in text
