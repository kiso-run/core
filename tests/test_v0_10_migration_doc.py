"""Invariants for ``docs/v0.10-migration.md``.

The v0.10 migration doc explains where every retired wrapper feature
now lives (MCP / skills / dropped) and maps every renamed
``kiso-run/wrapper-*`` repo to its new ``*-mcp`` counterpart. These
tests pin the doc's existence and required content so the retirement
stays navigable for anyone landing on v0.10 from v0.9.
"""

from __future__ import annotations

from pathlib import Path

import pytest


DOC_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "v0.10-migration.md"
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.is_file(), f"missing migration doc at {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


class TestMigrationDocBasics:

    def test_file_exists(self):
        assert DOC_PATH.is_file()

    def test_has_h1(self, doc_text: str):
        first_h1 = next(
            (line for line in doc_text.splitlines() if line.startswith("# ")),
            None,
        )
        assert first_h1 is not None, "migration doc must start with an H1"

    def test_mentions_retired_subsystems(self, doc_text: str):
        lowered = doc_text.lower()
        for keyword in ("wrapper", "recipe", "search"):
            assert keyword in lowered, (
                f"migration doc must mention retired subsystem '{keyword}'"
            )


class TestFeatureDispositionTable:
    """The feature-disposition table lists every wrapper feature and
    its new home (MCP method schema, per-session MCP pool, etc.).
    """

    def test_covers_wrapper_feature_surface(self, doc_text: str):
        lowered = doc_text.lower()
        for feature in (
            "args_schema",
            "session_secrets",
            "consumes",
            "usage_guide",
            "validator",
            "needs_install",
            "wrapper_recovery",
        ):
            assert feature in lowered, (
                f"migration doc must document disposition of '{feature}'"
            )

    def test_references_feature_ports(self, doc_text: str):
        # The doc points readers at the v0.10 features that host the
        # wrapper ports so they can trace where each feature actually
        # lands in code.
        lowered = doc_text.lower()
        for phrase in (
            "per-session mcp",
            "jsonschema",
            "x-kiso-consumes",
            "mcp_recovery",
        ):
            assert phrase in lowered, (
                f"migration doc must reference feature port '{phrase}'"
            )


class TestWrapperToMcpNameMapping:
    """Every renamed ``kiso-run/wrapper-*`` repo maps to a specific
    ``*-mcp`` repo. The doc enumerates this mapping explicitly.
    """

    @pytest.mark.parametrize(
        "old,new",
        [
            ("wrapper-aider", "aider-mcp"),
            ("wrapper-transcriber", "transcriber-mcp"),
            ("wrapper-ocr", "ocr-mcp"),
            ("wrapper-docreader", "docreader-mcp"),
        ],
    )
    def test_rename_mapping_listed(self, doc_text: str, old: str, new: str):
        assert old in doc_text, f"migration doc must list old repo '{old}'"
        assert new in doc_text, f"migration doc must list new repo '{new}'"

    def test_deleted_browser_wrapper_noted(self, doc_text: str):
        # kiso-run/wrapper-browser was dropped (superseded by
        # @playwright/mcp). The doc must say so.
        lowered = doc_text.lower()
        assert "wrapper-browser" in lowered
        assert "playwright" in lowered

    def test_new_search_mcp_noted(self, doc_text: str):
        # The built-in `search` task type was retired and replaced by
        # the new kiso-run/search-mcp server.
        assert "search-mcp" in doc_text
