"""Tests for functional test helpers (assert_italian, assert_url_reachable, etc).

These tests verify the helper functions themselves — they are NOT functional
tests and do NOT require --functional flag.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    FunctionalResult,
    assert_italian,
    assert_no_failure_language,
)


# ---------------------------------------------------------------------------
# assert_italian
# ---------------------------------------------------------------------------


class TestAssertItalian:
    def test_italian_text_passes(self):
        assert_italian("Ciao, questa è una prova del sistema di kiso")

    def test_english_text_raises(self):
        with pytest.raises(AssertionError, match="IT="):
            assert_italian("Hello, this is a test of the system")

    def test_mixed_text_italian_dominant(self):
        assert_italian(
            "Il sistema ha completato la task con successo. "
            "I file sono stati pubblicati nella cartella corretta."
        )

    def test_empty_text_raises(self):
        with pytest.raises(AssertionError):
            assert_italian("")

    def test_numbers_only_raises(self):
        with pytest.raises(AssertionError):
            assert_italian("12345 67890")

    def test_italian_with_technical_terms(self):
        # Technical terms (screenshot, browser) are neither IT nor EN
        # function words, so Italian articles/prepositions should win.
        assert_italian(
            "Ho fatto lo screenshot della pagina e il browser ha "
            "navigato sul sito con successo."
        )


# ---------------------------------------------------------------------------
# assert_no_failure_language
# ---------------------------------------------------------------------------


class TestAssertNoFailureLanguage:
    def test_clean_text_passes(self):
        assert_no_failure_language("Il sito è stato visitato con successo")

    def test_italian_failure_detected(self):
        with pytest.raises(AssertionError, match="(?i)non riesco"):
            assert_no_failure_language("Non riesco a navigare sul sito")

    def test_english_failure_detected(self):
        with pytest.raises(AssertionError, match="(?i)failed to"):
            assert_no_failure_language("Failed to connect to the server")

    def test_error_keyword_detected(self):
        with pytest.raises(AssertionError, match="errore"):
            assert_no_failure_language("Si è verificato un errore durante l'operazione")


# ---------------------------------------------------------------------------
# FunctionalResult
# ---------------------------------------------------------------------------


class TestFunctionalResult:
    def test_has_published_file_match(self):
        r = FunctionalResult(
            success=True,
            pub_files=[{"filename": "screenshot.png", "url": "http://x/pub/t/screenshot.png"}],
        )
        assert r.has_published_file("*.png")
        assert not r.has_published_file("*.md")

    def test_has_published_file_no_files(self):
        r = FunctionalResult(success=True)
        assert not r.has_published_file("*.png")

    def test_task_types(self):
        r = FunctionalResult(
            success=True,
            tasks=[
                {"type": "exec", "status": "done"},
                {"type": "skill", "status": "done"},
                {"type": "msg", "status": "done"},
            ],
        )
        assert r.task_types() == ["exec", "skill", "msg"]

    def test_tool_tasks(self):
        r = FunctionalResult(
            success=True,
            tasks=[
                {"type": "exec", "skill": None},
                {"type": "skill", "skill": "browser"},
                {"type": "msg", "skill": None},
            ],
        )
        tools = r.tool_tasks()
        assert len(tools) == 1
        assert tools[0]["skill"] == "browser"
