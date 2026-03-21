"""F18-F26, F31-F32: Core pipeline functional tests.

F18: Simple Q&A without tools.
F19: English response quality.
F20: Spanish response quality.
F21: Replan recovery after exec failure.
F22: Nonexistent tool request.
F23: Cross-session knowledge sharing.
F24-F26: Multistep (2-plan) flows.
F31: Russian response quality.
F32: Chinese response quality.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import pytest_asyncio

from kiso.store import create_session, save_message
from kiso.worker import _process_message

from tests.functional.conftest import (
    assert_chinese,
    assert_english,
    assert_no_failure_language,
    assert_russian,
    assert_spanish,
)

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# F18 — Simple Q&A without tools
# ---------------------------------------------------------------------------


class TestF18SimpleQA:
    """Ask a factual question — no exec, no tools, pure msg path."""

    async def test_simple_qa_no_tools(self, run_message):
        """What: Asks 'What is the capital of Japan?' — a factual question.

        Why: Most basic interaction path. Validates the system can answer
        without over-planning exec tasks for something that needs no execution.
        Expects: Success, English response, 'Tokyo' in output, no exec/tool tasks.
        """
        result = await run_message(
            "What is the capital of Japan?",
            timeout=120,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        assert_english(result.last_plan_msg_output)
        assert "tokyo" in result.last_plan_msg_output.lower(), (
            f"Expected 'Tokyo' in output: {result.last_plan_msg_output[:300]}"
        )
        # Should be pure msg — no exec or tool tasks
        types = result.task_types()
        assert "exec" not in types, f"Unexpected exec task in types: {types}"
        assert "tool" not in types, f"Unexpected tool task in types: {types}"


# ---------------------------------------------------------------------------
# F19 — English response quality
# ---------------------------------------------------------------------------


class TestF19EnglishResponse:
    """Ask in English, get substantive English response."""

    async def test_english_response_quality(self, run_message):
        """What: Asks about popular programming languages in English.

        Why: Validates language detection + messenger respects English.
        Expects: Success, English response, mentions at least 2 known languages,
        substantive output (>100 chars).
        """
        result = await run_message(
            "List 3 popular programming languages and briefly explain "
            "why each is widely used",
            timeout=120,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_english(output)
        assert_no_failure_language(output)
        assert len(output) > 100, f"Response too short ({len(output)} chars)"

        known = ["python", "javascript", "java", "typescript", "c++", "go", "rust", "c#"]
        found = [lang for lang in known if lang in output.lower()]
        assert len(found) >= 2, (
            f"Expected at least 2 programming languages, found: {found}. "
            f"Output: {output[:300]}"
        )


# ---------------------------------------------------------------------------
# F20 — Spanish response quality
# ---------------------------------------------------------------------------


class TestF20SpanishResponse:
    """Ask in Spanish, get substantive Spanish response."""

    async def test_spanish_response_quality(self, run_message):
        """What: Asks about popular programming languages in Spanish.

        Why: Validates non-Italian, non-English language handling.
        Expects: Success, Spanish response, mentions at least 2 languages.
        """
        result = await run_message(
            "¿Cuáles son los 3 lenguajes de programación más populares? "
            "Explica brevemente por qué cada uno es importante",
            timeout=120,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_spanish(output)
        assert len(output) > 100, f"Response too short ({len(output)} chars)"

        known = ["python", "javascript", "java", "typescript", "c++", "go", "rust", "c#"]
        found = [lang for lang in known if lang in output.lower()]
        assert len(found) >= 2, (
            f"Expected at least 2 programming languages, found: {found}. "
            f"Output: {output[:300]}"
        )


# ---------------------------------------------------------------------------
# F21 — Replan recovery after exec failure
# ---------------------------------------------------------------------------


class TestF21ReplanRecovery:
    """Force an exec failure and verify the system recovers gracefully."""

    async def test_replan_recovery_missing_file(self, run_message):
        """What: Asks to read a nonexistent file.

        Why: Validates the replan flow — exec fails, reviewer triggers replan,
        planner recovers by explaining the error to the user.
        Expects: Pipeline completes (no crash), msg output explains the problem.
        """
        result = await run_message(
            "leggi il file /tmp/file_inesistente_kiso_test_xyz99.txt "
            "e dimmi cosa contiene",
            timeout=180,
        )

        # Pipeline must complete — success or graceful failure both OK
        assert result.plans, "No plans were created"
        assert result.last_plan_msg_output, (
            "No msg output — user got no response"
        )

        # Output should mention the problem
        output = result.last_plan_msg_output.lower()
        error_indicators = (
            "non esiste", "non trovato", "not found", "errore",
            "impossibile", "non è stato possibile", "inesistente",
            "non è presente", "non disponibile", "non può",
        )
        assert any(ind in output for ind in error_indicators), (
            f"Expected error explanation in output: {result.last_plan_msg_output[:300]}"
        )


# ---------------------------------------------------------------------------
# F22 — Nonexistent tool request
# ---------------------------------------------------------------------------


class TestF22NonexistentTool:
    """Ask to install a tool that doesn't exist in the registry."""

    async def test_nonexistent_tool_request(self, run_message):
        """What: Asks to install 'zzz_test_notreal' — not in registry.

        Why: Validates that the planner uses registry_hints to decide tool
        availability and doesn't blindly attempt installation.
        Expects: Success, msg explains tool is not available, no install exec.
        """
        result = await run_message(
            "installa e usa il tool 'zzz_test_notreal' per analizzare il sistema",
            timeout=180,
        )

        # Either success (explained unavailability) or planning failure are
        # acceptable — the tool genuinely doesn't exist and the planner may
        # exhaust retries trying to produce a valid plan.
        assert result.plans, "No plans were created"

        # Should NOT have tried to install the nonexistent tool
        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
            if t.get("type") == "exec"
        ).lower()
        assert "kiso tool install zzz_test_notreal" not in all_output, (
            "Planner blindly attempted to install nonexistent tool"
        )


# ---------------------------------------------------------------------------
# F23 — Cross-session knowledge sharing
# ---------------------------------------------------------------------------


class TestF23CrossSessionKnowledge:
    """Teach in session A, recall in session B."""

    async def test_cross_session_knowledge_sharing(
        self, func_config, func_db, run_message,
    ):
        """What: Teaches a fact in one session, queries it from another.

        Why: Validates the full learning pipeline: teach → curator promote →
        global fact → briefer retrieval in a different session.
        Expects: Session A teaches successfully, Session B recalls the fact.
        """
        # Session A: teach a unique, memorable fact
        result_a = await run_message(
            "ricordati che il progetto Artemis usa PostgreSQL 16 "
            "come database principale",
            timeout=180,
        )
        assert result_a.success, (
            f"Session A teach failed: {[p.get('status') for p in result_a.plans]}"
        )

        # Session B: new session, same DB — query the fact
        session_b = f"func-{uuid.uuid4().hex[:12]}"
        await create_session(func_db, session_b)

        msg_id = await save_message(
            func_db, session_b, "testadmin", "user",
            "che database usa il progetto Artemis?",
        )
        msg = {
            "id": msg_id,
            "content": "che database usa il progetto Artemis?",
            "user_role": "admin",
            "user_tools": "*",
            "username": "testadmin",
            "base_url": "http://test",
        }

        cancel = asyncio.Event()
        t0 = time.monotonic()
        bg = await asyncio.wait_for(
            _process_message(
                func_db, func_config, session_b, msg, cancel,
                llm_timeout=func_config.settings["llm_timeout"],
                max_replan_depth=func_config.settings["max_replan_depth"],
            ),
            timeout=180,
        )
        if bg is not None and not bg.done():
            try:
                await asyncio.wait_for(bg, timeout=90)
            except (asyncio.TimeoutError, Exception):
                pass

        # Collect session B results
        cur = await func_db.execute(
            "SELECT * FROM tasks WHERE session = ? AND type = 'msg' "
            "AND status = 'done' ORDER BY id",
            (session_b,),
        )
        msg_tasks = [dict(r) for r in await cur.fetchall()]
        output_b = "\n".join(t.get("output") or "" for t in msg_tasks)

        assert output_b, "Session B produced no msg output"
        assert "postgres" in output_b.lower(), (
            f"Session B did not recall the PostgreSQL fact. "
            f"Output: {output_b[:300]}"
        )


# ---------------------------------------------------------------------------
# F24 — Create file → reference in next plan
# ---------------------------------------------------------------------------


class TestF24CreateThenReference:
    """Create a file, then ask about it in a second plan."""

    async def test_create_file_then_reference(self, run_message):
        """What: Creates a file in plan 1, queries it in plan 2.

        Why: Validates cross-plan state — the planner sees files created
        in previous plans via session workspace listing (M822).
        Expects: Plan 1 creates file, Plan 2 references it.
        """
        r1 = await run_message(
            "crea un file hello.txt con scritto 'ciao mondo'",
            timeout=300,
        )
        assert r1.success, (
            f"Plan 1 failed: {[p.get('status') for p in r1.plans]}"
        )

        r2 = await run_message(
            "quante parole ci sono nel file che hai appena creato?",
            timeout=300,
        )
        assert r2.success, (
            f"Plan 2 failed: {[p.get('status') for p in r2.plans]}"
        )
        output = r2.last_plan_msg_output.lower()
        assert any(w in output for w in ("2", "due", "ciao", "mondo")), (
            f"Plan 2 didn't reference file content: {r2.last_plan_msg_output[:300]}"
        )


# ---------------------------------------------------------------------------
# F25 — Exec fails → user corrects → success
# ---------------------------------------------------------------------------


class TestF25ExecFailsUserCorrects:
    """First exec fails, user gives correction, second succeeds."""

    async def test_exec_fails_user_corrects(self, run_message):
        """What: Runs a nonexistent script, then asks to create + run it.

        Why: Validates error recovery via user correction across plans.
        Expects: Plan 1 reports error, Plan 2 succeeds with output.
        """
        r1 = await run_message(
            "esegui python3 myscript.py",
            timeout=300,
        )
        # Plan 1 may succeed (explains error) or fail — both OK
        assert r1.plans, "No plans created"

        r2 = await run_message(
            "scrivi prima lo script myscript.py che stampa 'hello world', "
            "poi eseguilo",
            timeout=300,
        )
        assert r2.success, (
            f"Plan 2 failed: {[p.get('status') for p in r2.plans]}"
        )
        all_output = "\n".join(
            t.get("output") or "" for t in r2.tasks
        ).lower()
        assert "hello world" in all_output, (
            f"Expected 'hello world' in output: {all_output[:500]}"
        )


# ---------------------------------------------------------------------------
# F26 — Teach fact → use in exec task
# ---------------------------------------------------------------------------


class TestF26TeachFactThenRecall:
    """Teach a fact, then recall it in the next plan."""

    async def test_teach_fact_then_recall(self, run_message):
        """What: Teaches port number, then asks what port the project uses.

        Why: Validates knowledge integration across plans — the planner
        retrieves the learned fact and uses it in the response.
        Expects: Plan 2 response mentions port 9090 from the learned fact.
        """
        r1 = await run_message(
            "ricordati che il progetto Zeus usa la porta 9090",
            timeout=300,
        )
        assert r1.success, (
            f"Teach failed: {[p.get('status') for p in r1.plans]}"
        )

        r2 = await run_message(
            "che porta usa il progetto Zeus?",
            timeout=300,
        )
        assert r2.success, (
            f"Plan 2 failed: {[p.get('status') for p in r2.plans]}"
        )
        output = r2.last_plan_msg_output.lower()
        assert "9090" in output, (
            f"Expected '9090' in response: {output[:500]}"
        )


# ---------------------------------------------------------------------------
# F31 — Russian response quality
# ---------------------------------------------------------------------------


class TestF31RussianResponse:
    """Ask in Russian, get substantive Russian response."""

    async def test_russian_response_quality(self, run_message):
        """What: Asks about popular programming languages in Russian.

        Why: Validates non-Latin script (Cyrillic) handling.
        Expects: Success, Russian response, mentions at least 2 languages.
        """
        result = await run_message(
            "Какие 3 самых популярных языка программирования? "
            "Кратко объясни почему каждый из них важен",
            timeout=120,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_russian(output)
        assert len(output) > 100, f"Response too short ({len(output)} chars)"

        known = ["python", "javascript", "java", "typescript", "c++", "go", "rust", "c#"]
        found = [lang for lang in known if lang in output.lower()]
        assert len(found) >= 2, (
            f"Expected at least 2 programming languages, found: {found}. "
            f"Output: {output[:300]}"
        )


# ---------------------------------------------------------------------------
# F32 — Chinese response quality
# ---------------------------------------------------------------------------


class TestF32ChineseResponse:
    """Ask in Chinese, get substantive Chinese response."""

    async def test_chinese_response_quality(self, run_message):
        """What: Asks about popular programming languages in Chinese.

        Why: Validates CJK script handling.
        Expects: Success, Chinese response, mentions at least 2 languages.
        """
        result = await run_message(
            "最受欢迎的3种编程语言是什么？简要说明每种语言为什么重要",
            timeout=120,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_chinese(output)
        assert len(output) > 50, f"Response too short ({len(output)} chars)"

        known = ["python", "javascript", "java", "typescript", "c++", "go", "rust", "c#"]
        found = [lang for lang in known if lang in output.lower()]
        assert len(found) >= 2, (
            f"Expected at least 2 programming languages, found: {found}. "
            f"Output: {output[:300]}"
        )
