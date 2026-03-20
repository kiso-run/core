"""Tests for functional test helpers (assert_italian, assert_url_reachable, etc).

These tests verify the helper functions themselves — they are NOT functional
tests and do NOT require --functional flag.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    FunctionalResult,
    assert_english,
    assert_italian,
    assert_language,
    assert_no_failure_language,
    assert_spanish,
)


# ---------------------------------------------------------------------------
# assert_language / assert_italian / assert_english / assert_spanish
# ---------------------------------------------------------------------------


class TestAssertItalian:
    def test_italian_text_passes(self):
        assert_italian("Ciao, questa è una prova del sistema di kiso")

    def test_english_text_raises(self):
        with pytest.raises(AssertionError, match="Italian"):
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

    def test_italian_with_code_block(self):
        # Code block keywords (in, for, not, is) should not skew EN score.
        assert_italian(
            "Ecco uno script che calcola i numeri di Fibonacci:\n\n"
            "```python\n"
            "def fibonacci(n):\n"
            "    a, b = 0, 1\n"
            "    for _ in range(n):\n"
            "        a, b = b, a + b\n"
            "    return a\n"
            "```\n\n"
            "Il risultato per i primi 20 numeri della sequenza è il seguente."
        )


class TestAssertEnglish:
    def test_english_text_passes(self):
        assert_english("This is a test of the system and it should work")

    def test_italian_text_raises(self):
        with pytest.raises(AssertionError, match="English"):
            assert_english("Il sistema ha completato la task con successo")

    def test_spanish_text_raises(self):
        with pytest.raises(AssertionError, match="English"):
            assert_english("Este es un sistema para el análisis de datos")


class TestAssertSpanish:
    def test_spanish_text_passes(self):
        assert_spanish(
            "Este es un sistema para el análisis de los datos más importantes"
        )

    def test_english_text_raises(self):
        with pytest.raises(AssertionError, match="Spanish"):
            assert_spanish("This is a test of the system and it should work")

    def test_italian_text_raises(self):
        with pytest.raises(AssertionError, match="Spanish"):
            assert_spanish("Il sistema ha completato la task con successo")


class TestAssertLanguage:
    def test_direct_call_it(self):
        assert_language("Ciao, questa è una prova del sistema", "it")

    def test_direct_call_en(self):
        assert_language("This is a test of the system", "en")

    def test_direct_call_es(self):
        assert_language("Este es un sistema para el análisis de datos", "es")


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

    def test_failure_language_in_code_block_ignored(self):
        # "failed to" inside a code block should not trigger.
        assert_no_failure_language(
            "Ecco il risultato della ricerca:\n\n"
            "```python\n"
            "if response.status != 200:\n"
            "    raise RuntimeError('failed to fetch')\n"
            "```\n\n"
            "Il programma funziona correttamente."
        )

    def test_failure_language_outside_code_block_detected(self):
        with pytest.raises(AssertionError, match="(?i)failed to"):
            assert_no_failure_language("The task failed to complete.")

    def test_failure_in_bullet_point_ignored(self):
        # "errore" inside a markdown list item (scraped content) should not trigger.
        assert_no_failure_language(
            "Ecco le ultime notizie:\n\n"
            "*   **INTER:** Frenata, errore arbitrale nel mirino\n"
            "*   **MILAN:** Mercato inatteso\n\n"
            "Queste sono le notizie principali di oggi."
        )

    def test_failure_in_numbered_list_ignored(self):
        assert_no_failure_language(
            "I risultati della ricerca:\n\n"
            "1. Errore di Verstappen al GP di Monaco\n"
            "2. Hamilton vince la gara\n\n"
            "Ecco il riepilogo."
        )

    def test_failure_in_blockquote_ignored(self):
        assert_no_failure_language(
            "Dal comunicato stampa:\n\n"
            "> Si è verificato un errore nel sistema di voto\n\n"
            "La situazione è stata risolta."
        )

    def test_failure_in_prose_still_caught(self):
        # "errore" in regular prose (not a list/quote) must still be caught.
        with pytest.raises(AssertionError, match="errore"):
            assert_no_failure_language(
                "Si è verificato un errore durante la navigazione."
            )


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
                {"type": "tool", "status": "done"},
                {"type": "msg", "status": "done"},
            ],
        )
        assert r.task_types() == ["exec", "tool", "msg"]

    def test_tool_tasks(self):
        r = FunctionalResult(
            success=True,
            tasks=[
                {"type": "exec", "tool": None},
                {"type": "tool", "tool": "browser"},
                {"type": "msg", "tool": None},
            ],
        )
        tools = r.tool_tasks()
        assert len(tools) == 1
        assert tools[0]["tool"] == "browser"

    def test_last_plan_msg_output_single_plan(self):
        r = FunctionalResult(
            success=True,
            plans=[{"id": 1}],
            tasks=[
                {"type": "msg", "status": "done", "output": "hello", "plan_id": 1},
            ],
        )
        assert r.last_plan_msg_output == "hello"

    def test_last_plan_msg_output_multi_plan(self):
        """Only includes msg output from the last plan."""
        r = FunctionalResult(
            success=True,
            plans=[{"id": 1}, {"id": 2}],
            tasks=[
                {"type": "msg", "status": "done", "output": "install proposal", "plan_id": 1},
                {"type": "exec", "status": "done", "output": "ok", "plan_id": 2},
                {"type": "msg", "status": "done", "output": "risultati", "plan_id": 2},
            ],
        )
        assert r.last_plan_msg_output == "risultati"
        assert "install proposal" not in r.last_plan_msg_output

    def test_last_plan_msg_output_empty(self):
        r = FunctionalResult(success=False)
        assert r.last_plan_msg_output == ""
