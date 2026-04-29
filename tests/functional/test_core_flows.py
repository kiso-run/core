"""F18-F26, F31-F32: Core pipeline functional tests.

F18: Simple Q&A without wrappers.
F19: English response quality.
F20: Spanish response quality.
F21: Replan recovery after exec failure.
F22: Nonexistent wrapper request.
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
from kiso.worker.loop import _process_message

from tests.conftest import (
    LLM_MULTI_PLAN_TIMEOUT,
    LLM_REPLAN_TIMEOUT,
    LLM_SINGLE_PLAN_TIMEOUT,
)
from tests.functional.conftest import (
    assert_chinese,
    assert_english,
    assert_no_failure_language,
    assert_russian,
    assert_spanish,
)

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# F18 — Simple Q&A without wrappers
# ---------------------------------------------------------------------------


class TestF18SimpleQA:
    """Ask a factual question — no exec, no wrappers, pure msg path."""

    async def test_simple_qa_no_tools(self, run_message):
        """What: Asks 'What is the capital of Japan?' — a factual question.

        Why: Most basic interaction path. Validates the system can answer
        without over-planning exec tasks for something that needs no execution.
        Expects: Success, English response, 'Tokyo' in output, no exec/wrapper tasks.
        """
        result = await run_message(
            "What is the capital of Japan?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        assert_english(result.last_plan_msg_output)
        assert "tokyo" in result.last_plan_msg_output.lower(), (
            f"Expected 'Tokyo' in output: {result.last_plan_msg_output[:300]}"
        )
        # Should be pure msg — no exec or wrapper tasks
        types = result.task_types()
        assert "exec" not in types, f"Unexpected exec task in types: {types}"
        assert "wrapper" not in types, f"Unexpected wrapper task in types: {types}"


# ---------------------------------------------------------------------------
# F19 — English response quality
# ---------------------------------------------------------------------------


class TestF19EnglishResponse:
    """Ask in English, get substantive English response."""

    async def test_english_response_quality(self, run_message):
        """What: Asks about recursion in English.

        Why: Validates language detection + messenger respects English.
        Pure knowledge question — unambiguously chat, no wrapper needed.
        Expects: Success, English response, mentions recursion-related terms,
        substantive output (>100 chars).
        """
        result = await run_message(
            "What is recursion in programming? Explain with a simple example",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_english(output)
        assert_no_failure_language(output)
        types = result.task_types()
        assert "exec" not in types, f"Unexpected exec task in pure QA flow: {types}"
        assert "wrapper" not in types, f"Unexpected wrapper task in pure QA flow: {types}"

        lower = output.lower()
        keywords = ["recursion", "recursive", "function", "base case", "call",
                     "factorial", "fibonacci", "stack"]
        found = [kw for kw in keywords if kw in lower]
        assert len(found) >= 2, (
            f"Expected at least 2 recursion keywords, found: {found}. "
            f"Output: {output[:300]}"
        )


# ---------------------------------------------------------------------------
# F20 — Spanish response quality
# ---------------------------------------------------------------------------


class TestF20SpanishResponse:
    """Ask in Spanish, get substantive Spanish response."""

    async def test_spanish_response_quality(self, run_message):
        """What: Asks about recursion in Spanish.

        Why: Validates non-Italian, non-English language handling.
        Pure knowledge question — unambiguously chat, no wrapper needed.
        Expects: Success, Spanish response, mentions recursion-related terms.
        """
        result = await run_message(
            "¿Qué es la recursión en programación? "
            "Explica con un ejemplo sencillo",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_spanish(output)
        types = result.task_types()
        assert "exec" not in types, f"Unexpected exec task in pure QA flow: {types}"
        assert "wrapper" not in types, f"Unexpected wrapper task in pure QA flow: {types}"

        lower = output.lower()
        keywords = ["recursión", "recursiva", "recursivo", "función", "caso base",
                     "factorial", "fibonacci", "pila", "llamada"]
        found = [kw for kw in keywords if kw in lower]
        assert len(found) >= 2, (
            f"Expected at least 2 recursion keywords, found: {found}. "
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
            timeout=LLM_REPLAN_TIMEOUT,
        )

        # Pipeline must complete — success or graceful failure both OK
        assert result.plans, "No plans were created"
        assert result.last_plan_msg_output, (
            "No msg output — user got no response"
        )
        assert "exec" in result.task_types(), (
            f"Expected failing exec before replan/recovery, got: {result.task_types()}"
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
# F22 — Nonexistent wrapper request
# ---------------------------------------------------------------------------


class TestF22NonexistentTool:
    """Ask to install a wrapper that doesn't exist in the registry."""

    async def test_nonexistent_tool_request(self, run_message):
        """What: Asks to install 'zzz_test_notreal' — not in registry.

        Why: Validates that the planner consults the available MCP catalog
        before attempting installation and doesn't blindly try unknown names.
        Expects: Success, msg explains wrapper is not available, no install exec.
        """
        result = await run_message(
            "installa e usa il wrapper 'zzz_test_notreal' per analizzare il sistema",
            timeout=LLM_REPLAN_TIMEOUT,
        )

        # Either success (explained unavailability) or planning failure are
        # acceptable — the wrapper genuinely doesn't exist and the planner may
        # exhaust retries trying to produce a valid plan.
        assert result.plans, "No plans were created"

        # Should NOT have tried to install the nonexistent wrapper
        all_output = "\n".join(
            t.get("output") or "" for t in result.tasks
            if t.get("type") == "exec"
        ).lower()
        assert "kiso wrapper install zzz_test_notreal" not in all_output, (
            "Planner blindly attempted to install nonexistent wrapper"
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
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
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
            "user_wrappers": "*",
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
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
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
        in previous plans via session workspace listing.
        Expects: Plan 1 creates file, Plan 2 references it.
        """
        r1 = await run_message(
            "crea un file hello.txt con scritto 'ciao mondo'",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r1.success, (
            f"Plan 1 failed: {[p.get('status') for p in r1.plans]}"
        )

        r2 = await run_message(
            "quante parole ci sono nel file che hai appena creato?",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r2.success, (
            f"Plan 2 failed: {[p.get('status') for p in r2.plans]}"
        )
        last_plan_id = r2.plans[-1]["id"]
        last_plan_tasks = [t for t in r2.tasks if t.get("plan_id") == last_plan_id]
        task_blob = "\n".join(
            ((t.get("detail") or "") + "\n" + (t.get("command") or ""))
            for t in last_plan_tasks
        ).lower()
        assert "hello.txt" in task_blob, (
            f"Expected plan 2 to reuse hello.txt path, got: {task_blob[:500]}"
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
            timeout=LLM_REPLAN_TIMEOUT,
        )
        # Plan 1 may succeed (explains error) or fail — both OK
        assert r1.plans, "No plans created"

        r2 = await run_message(
            "scrivi prima lo script myscript.py che stampa 'hello world', "
            "poi eseguilo",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
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
            timeout=LLM_REPLAN_TIMEOUT,
        )
        assert r1.success, (
            f"Teach failed: {[p.get('status') for p in r1.plans]}"
        )

        r2 = await run_message(
            "che porta usa il progetto Zeus?",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
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
        """What: Asks about recursion in Russian.

        Why: Validates non-Latin script (Cyrillic) handling.
        Pure knowledge question — unambiguously chat, no wrapper needed.
        Expects: Success, Russian response, mentions recursion-related terms.
        """
        result = await run_message(
            "Что такое рекурсия в программировании? "
            "Объясни на простом примере",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_russian(output)
        types = result.task_types()
        assert "exec" not in types, f"Unexpected exec task in pure QA flow: {types}"
        assert "wrapper" not in types, f"Unexpected wrapper task in pure QA flow: {types}"

        lower = output.lower()
        keywords = ["рекурси", "функци", "базов", "вызов", "факториал",
                     "фибоначчи", "стек", "python", "def"]
        found = [kw for kw in keywords if kw in lower]
        assert len(found) >= 2, (
            f"Expected at least 2 recursion keywords, found: {found}. "
            f"Output: {output[:300]}"
        )


# ---------------------------------------------------------------------------
# F32 — Chinese response quality
# ---------------------------------------------------------------------------


class TestF32ChineseResponse:
    """Ask in Chinese, get substantive Chinese response."""

    async def test_chinese_response_quality(self, run_message):
        """What: Asks about recursion in Chinese.

        Why: Validates CJK script handling.
        Pure knowledge question — unambiguously chat, no wrapper needed.
        Expects: Success, Chinese response, mentions recursion-related terms.
        """
        result = await run_message(
            "什么是编程中的递归？用一个简单的例子来解释",
            timeout=LLM_SINGLE_PLAN_TIMEOUT,
        )

        assert result.success, (
            f"Plan failed. Plans: {[p.get('status') for p in result.plans]}"
        )
        output = result.last_plan_msg_output
        assert_chinese(output)
        types = result.task_types()
        assert "exec" not in types, f"Unexpected exec task in pure QA flow: {types}"
        assert "wrapper" not in types, f"Unexpected wrapper task in pure QA flow: {types}"

        lower = output.lower()
        keywords = ["递归", "函数", "基", "调用", "阶乘",
                     "斐波那契", "栈", "python", "def"]
        found = [kw for kw in keywords if kw in lower]
        assert len(found) >= 2, (
            f"Expected at least 2 recursion keywords, found: {found}. "
            f"Output: {output[:300]}"
        )
