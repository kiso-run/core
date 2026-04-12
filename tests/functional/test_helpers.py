"""Tests for functional test helpers (assert_italian, assert_url_reachable, etc).

These tests verify the helper functions themselves — they are NOT functional
tests and do NOT require --functional flag.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import (
    FunctionalResult,
    assert_chinese,
    assert_english,
    assert_italian,
    assert_language,
    assert_no_failure_language,
    assert_russian,
    assert_spanish,
    normalize_for_assertion,
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

    def test_direct_call_ru(self):
        assert_language("Это тест системы и он должен работать правильно", "ru")

    def test_direct_call_zh(self):
        assert_language("这是一个系统测试它应该正常工作的结果", "zh")


class TestAssertRussian:
    def test_russian_text_passes(self):
        assert_russian("Это тест системы и он должен работать правильно")

    def test_english_text_raises(self):
        with pytest.raises(AssertionError, match="Russian"):
            assert_russian("This is a test of the system")


class TestAssertChinese:
    def test_chinese_text_passes(self):
        assert_chinese("这是一个系统测试它应该正常工作的结果说明")

    def test_english_text_raises(self):
        with pytest.raises(AssertionError, match="Chinese"):
            assert_chinese("This is a test with no CJK characters")


class TestNormalizeForAssertion:
    def test_latin_strips_accents(self):
        assert normalize_for_assertion("París café") == "paris cafe"

    def test_latin_lowercases(self):
        assert normalize_for_assertion("HELLO World") == "hello world"

    def test_non_latin_preserves_cyrillic(self):
        result = normalize_for_assertion("Москва", latin=False)
        assert "москва" in result

    def test_non_latin_no_accent_strip(self):
        # Cyrillic й should NOT be stripped (it's not a combining accent)
        result = normalize_for_assertion("Россий", latin=False)
        assert "россий" in result


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
                {"type": "wrapper", "status": "done"},
                {"type": "msg", "status": "done"},
            ],
        )
        assert r.task_types() == ["exec", "tool", "msg"]

    def test_wrapper_tasks(self):
        r = FunctionalResult(
            success=True,
            tasks=[
                {"type": "exec", "wrapper": None},
                {"type": "wrapper", "wrapper": "browser"},
                {"type": "msg", "wrapper": None},
            ],
        )
        tools = r.tool_tasks()
        assert len(tools) == 1
        assert tools[0]["wrapper"] == "browser"

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


# ---------------------------------------------------------------------------
# assert_no_command_word — M1286
# ---------------------------------------------------------------------------


class TestAssertNoCommandWord:
    """Verify the command-vs-data assertion helper:

    - inspects only the `command` field of `exec` tasks
    - uses word boundaries so "curly", "libcurl", etc. don't match
    - ignores `detail` (heredoc bodies, planner reasoning text)
    - ignores non-exec tasks
    """

    def test_exec_task_with_curl_command_fails(self):
        from tests.functional.conftest import assert_no_command_word
        tasks = [{"type": "exec", "command": "curl http://example.com",
                  "detail": "fetch data"}]
        with pytest.raises(AssertionError, match="curl"):
            assert_no_command_word(tasks, ["curl", "wget"])

    def test_exec_task_with_wget_command_fails(self):
        from tests.functional.conftest import assert_no_command_word
        tasks = [{"type": "exec", "command": "wget -q https://x.test",
                  "detail": ""}]
        with pytest.raises(AssertionError, match="wget"):
            assert_no_command_word(tasks, ["curl", "wget"])

    def test_curly_in_detail_does_not_match_curl_word(self):
        """Regression for the M1286 false positive: 'curly brackets'
        in heredoc body must NOT match 'curl' as a command word."""
        from tests.functional.conftest import assert_no_command_word
        tasks = [
            {
                "type": "exec",
                "command": "python3 text_stats.py",
                "detail": (
                    "python3 text_stats.py << 'eof'\n"
                    "Python uses indentation rather than curly brackets\n"
                    "eof"
                ),
            }
        ]
        # Must not raise — 'curl' must NOT match 'curly'
        assert_no_command_word(tasks, ["curl", "wget"])

    def test_libcurl_in_command_does_not_match_curl_word(self):
        from tests.functional.conftest import assert_no_command_word
        tasks = [{"type": "exec", "command": "gcc -lcurl libcurl_demo.c",
                  "detail": ""}]
        # 'curl' must not match 'libcurl' (no word boundary on the left)
        assert_no_command_word(tasks, ["curl"])

    def test_msg_task_ignored(self):
        from tests.functional.conftest import assert_no_command_word
        tasks = [
            {"type": "msg", "command": "curl http://anything", "detail": ""},
        ]
        # Only exec tasks are inspected
        assert_no_command_word(tasks, ["curl"])

    def test_empty_task_list(self):
        from tests.functional.conftest import assert_no_command_word
        assert_no_command_word([], ["curl"])

    def test_task_with_no_command(self):
        from tests.functional.conftest import assert_no_command_word
        tasks = [{"type": "exec", "detail": "some detail"}]
        assert_no_command_word(tasks, ["curl"])

    def test_assertion_message_includes_command_and_word(self):
        from tests.functional.conftest import assert_no_command_word
        tasks = [{"type": "exec", "command": "curl --silent http://x"}]
        with pytest.raises(AssertionError) as exc:
            assert_no_command_word(tasks, ["curl"])
        assert "curl" in str(exc.value)

    def test_word_boundary_at_end_too(self):
        """'curly' at end of word also must not match 'curl'."""
        from tests.functional.conftest import assert_no_command_word
        tasks = [{"type": "exec", "command": "echo curly_brace_test"}]
        assert_no_command_word(tasks, ["curl"])

    def test_curl_as_subcommand_argument_still_matches(self):
        """Defensive: 'curl' as a real shell token in the command
        should still trigger, even mid-command."""
        from tests.functional.conftest import assert_no_command_word
        tasks = [{"type": "exec",
                  "command": "bash -c 'curl http://x | jq .'"}]
        with pytest.raises(AssertionError):
            assert_no_command_word(tasks, ["curl"])


# ---------------------------------------------------------------------------
# drive_install_flow — M1286
# ---------------------------------------------------------------------------


class TestDriveInstallFlow:
    """Verify the drive-until-done install loop:

    - if tool already installed when called, sends prompt once and
      returns
    - if tool installs after one follow-up, re-issues prompt so the
      result reflects the installed-tool path
    - if tool never installs, gives up at max_turns and returns the
      last result so the caller's assertion shows what went wrong
    - does not constrain what the planner does — just drives the
      conversation forward
    """

    async def test_tool_already_installed_returns_after_first_call(self):
        from unittest.mock import patch
        from tests.functional.conftest import drive_install_flow

        calls: list[str] = []

        async def fake_run(content, **kwargs):
            calls.append(content)
            return f"result-of:{content}"

        with patch(
            "tests.functional.conftest.tool_installed", return_value=True
        ):
            result = await drive_install_flow(
                fake_run, "browser", "do something", max_turns=4,
            )

        # 2 calls: original + re-issued original after install confirmed
        assert calls == ["do something", "do something"]
        assert result == "result-of:do something"

    async def test_installs_after_one_followup(self):
        from unittest.mock import patch
        from tests.functional.conftest import drive_install_flow

        calls: list[str] = []
        # tool_installed returns False initially, then True after the
        # follow-up message
        installed_state = {"value": False}

        def fake_installed(name):
            return installed_state["value"]

        async def fake_run(content, **kwargs):
            calls.append(content)
            if "installa il tool" in content:
                installed_state["value"] = True
            return f"result-of:{content}"

        with patch(
            "tests.functional.conftest.tool_installed",
            side_effect=fake_installed,
        ):
            result = await drive_install_flow(
                fake_run, "browser", "do something", max_turns=4,
            )

        assert calls == [
            "do something",
            "sì, installa il tool browser",
            "do something",
        ]
        # final result is the re-issued prompt
        assert result == "result-of:do something"

    async def test_installs_after_two_followups(self):
        from unittest.mock import patch
        from tests.functional.conftest import drive_install_flow

        calls: list[str] = []
        followup_count = {"n": 0}

        def fake_installed(name):
            return followup_count["n"] >= 2

        async def fake_run(content, **kwargs):
            calls.append(content)
            if "installa il tool" in content:
                followup_count["n"] += 1
            return f"result-of:{content}"

        with patch(
            "tests.functional.conftest.tool_installed",
            side_effect=fake_installed,
        ):
            await drive_install_flow(
                fake_run, "ocr", "extract text", max_turns=4,
            )

        # original + 2 follow-ups + re-issued original
        assert calls == [
            "extract text",
            "sì, installa il tool ocr",
            "sì, installa il tool ocr",
            "extract text",
        ]

    async def test_never_installs_gives_up_at_max_turns(self):
        from unittest.mock import patch
        from tests.functional.conftest import drive_install_flow

        calls: list[str] = []

        async def fake_run(content, **kwargs):
            calls.append(content)
            return f"result-of:{content}"

        with patch(
            "tests.functional.conftest.tool_installed", return_value=False
        ):
            result = await drive_install_flow(
                fake_run, "aider", "go", max_turns=3,
            )

        # 1 original + 2 follow-ups (max_turns=3 total user turns), no
        # final re-issued prompt because the wrapper never installed
        assert len(calls) == 3
        assert calls[0] == "go"
        assert all(
            "installa il tool aider" in c for c in calls[1:]
        )
        # caller still gets a result (the last follow-up's response)
        # so their assertion can show diagnostic state
        assert result is not None

    async def test_default_timeout_is_install_timeout(self):
        """Regression: default timeout MUST be LLM_INSTALL_TIMEOUT
        (not run_message's own default of 300s). The install plan
        downloads multi-hundred-MB packages and runs deps.sh — 300s
        is not enough."""
        from unittest.mock import patch
        from tests.functional.conftest import drive_install_flow
        from tests.conftest import LLM_INSTALL_TIMEOUT

        captured_kwargs: list[dict] = []

        async def fake_run(content, **kwargs):
            captured_kwargs.append(dict(kwargs))
            return f"result-of:{content}"

        with patch(
            "tests.functional.conftest.tool_installed", return_value=True
        ):
            await drive_install_flow(
                fake_run, "browser", "do something", max_turns=4,
            )

        # Every call must have received the install-sized timeout
        assert all(
            kw.get("timeout") == LLM_INSTALL_TIMEOUT for kw in captured_kwargs
        ), f"timeout not propagated correctly: {captured_kwargs}"

    async def test_does_not_constrain_planner_phrasing(self):
        """The follow-up phrasing is the helper's choice — but the
        helper itself never inspects what the planner did or
        what role the run_message returned. It only checks
        tool_installed() between turns."""
        from unittest.mock import patch
        from tests.functional.conftest import drive_install_flow

        async def fake_run(content, **kwargs):
            # planner could return literally anything; helper does not
            # inspect the content
            return {"weird": "shape", "tasks": ["whatever"]}

        with patch(
            "tests.functional.conftest.tool_installed", return_value=True
        ):
            result = await drive_install_flow(
                fake_run, "browser", "anything", max_turns=4,
            )
        # No assertion on result shape — just doesn't crash
        assert result == {"weird": "shape", "tasks": ["whatever"]}
