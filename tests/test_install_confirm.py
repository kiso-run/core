"""M421/M435/M615/M670 — Integration tests for install confirmation flow.

End-to-end checks that the system prevents silent skill/connector
installation without user approval.  M615 adds server-side detection
of install proposals (replaces keyword heuristic).  M670 broadens
detection for msg-only plans on fresh instances.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from kiso.brain import (
    PlanError,
    _load_modular_prompt,
    _retry_llm_with_validation,
    run_planner,
    validate_plan,
)


# --- 1. Planner prompt rules reference user confirmation ---


class TestPlannerPromptInstallRules:
    """Planner prompt modules must enforce user-first confirmation."""

    @pytest.fixture(autouse=True)
    def _load_prompt(self):
        self.full = _load_modular_prompt(
            "planner",
            ["kiso_native", "tools_rules", "web", "plugin_install"],
        )

    def test_kiso_native_never_install_without_approval(self):
        assert "msg for approval" in self.full or "Never install anything" in self.full

    def test_tools_rules_msg_before_install(self):
        assert "single msg asking user to install" in self.full.lower() or \
               "single msg" in self.full.lower()

    def test_web_module_ask_before_install(self):
        assert "Single msg" in self.full or "single msg" in self.full.lower()

    def test_plugin_install_requires_prior_approval(self):
        assert "approved" in self.full.lower() or "consent" in self.full.lower()

    def test_m733_core_allows_system_packages(self):
        """M733/M849: core prompt allows system pkg manager for non-kiso packages."""
        core = _load_modular_prompt("planner", [])
        assert "System package requests" in core
        assert "uv pip install" in core
        assert "needs_install" in core

    def test_m733_tool_recovery_still_blocks_apt_for_deps(self):
        """M733: tool_recovery module still blocks apt-get for broken tool deps."""
        tool_recovery = _load_modular_prompt("planner", ["tool_recovery"])
        assert "Never apt-get/pip install to fix" in tool_recovery


# --- 2–4. validate_plan: install only in replan ---


class TestValidatePlanInstallConfirmation:
    """Install execs are blocked in first plan; only allowed in replan."""

    def test_install_in_first_plan_rejected(self):
        """M979: install + needs_install → blocked (mixed propose+install)."""
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "msg", "detail": "Answer in English. report results", "expect": None},
        ], "needs_install": ["browser"]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_msg_then_install_same_plan_rejected(self):
        """M979: msg + exec install + needs_install → rejected."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. Install browser?", "expect": None},
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ], "needs_install": ["browser"]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_replan_allows_install(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=True)
        assert not any("first plan" in e for e in errors)

    def test_connector_install_also_caught(self):
        """M979: connector install + needs_install → blocked."""
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso connector install slack", "expect": "ok"},
            {"type": "msg", "detail": "Answer in English. report results", "expect": None},
        ], "needs_install": ["slack"]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_msg_only_asking_to_install_accepted(self):
        """Plan with just a msg asking about install (no exec) → passes."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. Shall I install browser?", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert not any("first plan" in e for e in errors)


# --- 5. Capability gap text ---


# --- 6. Auto-correct function removed ---


class TestAutoCorrectRemoved:
    """_auto_correct_uninstalled_skills must no longer exist in brain.py."""

    def test_function_not_in_module(self):
        import kiso.brain as brain_mod
        assert not hasattr(brain_mod, "_auto_correct_uninstalled_skills")

    def test_regex_not_in_module(self):
        import kiso.brain as brain_mod
        assert not hasattr(brain_mod, "_SKILL_NOT_INSTALLED_RE")


# --- M428: session-aware install approval ---


class TestValidatePlanInstallApproved:
    """M428: install_approved=True allows install execs in first plan."""

    def test_install_approved_allows_first_plan_install(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=False, install_approved=True)
        assert not any("first plan" in e for e in errors)

    def test_install_not_approved_with_needs_install_blocks(self):
        """M979: not approved + needs_install → blocked."""
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "msg", "detail": "Answer in English. report results", "expect": None},
        ], "needs_install": ["browser"]}
        errors = validate_plan(plan, is_replan=False, install_approved=False)
        assert any("first plan" in e for e in errors)

    def test_replan_still_works_without_approval(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=True, install_approved=False)
        assert not any("first plan" in e for e in errors)


@pytest.mark.asyncio
class TestSessionHasInstallProposal:
    """M615: session_has_install_proposal checks install_proposal column."""

    async def test_empty_session_returns_false(self, tmp_path):
        from kiso.store import init_db, create_session, session_has_install_proposal
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()

    async def test_plan_with_install_proposal_returns_true(self, tmp_path):
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid = await create_plan(db, "sess1", msg_id, "Ask to install")
        await update_plan_install_proposal(db, pid)
        await create_task(db, pid, "sess1", "msg",
                          "Answer in English. Vuoi installare il browser?")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sess1") is True
        await db.close()

    async def test_plan_without_proposal_returns_false(self, tmp_path):
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid = await create_plan(db, "sess1", msg_id, "Just a msg")
        await create_task(db, pid, "sess1", "msg", "Here is the result.")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()


# --- M615: install-proposal edge cases (replaces M435 keyword tests) ---


@pytest.mark.asyncio
class TestInstallProposalEdgeCases:
    """M615: edge cases for install_proposal column detection."""

    async def test_different_session_not_counted(self, tmp_path):
        """Proposal in session A should not affect session B."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sessA")
        await create_session(db, "sessB")
        msg_id = await save_message(db, "sessA", "alice", "user", "hi")
        pid = await create_plan(db, "sessA", msg_id, "Ask install")
        await update_plan_install_proposal(db, pid)
        await create_task(db, pid, "sessA", "msg", "Install browser?")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sessA") is True
        assert await session_has_install_proposal(db, "sessB") is False
        await db.close()

    async def test_latest_plan_wins(self, tmp_path):
        """A newer non-proposal plan overrides an older proposal."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid1 = await create_plan(db, "sess1", msg_id, "Propose install")
        await update_plan_install_proposal(db, pid1)
        await create_task(db, pid1, "sess1", "msg", "Install browser?")
        await update_plan_status(db, pid1, "done")
        assert await session_has_install_proposal(db, "sess1") is True
        # Newer plan without proposal
        pid2 = await create_plan(db, "sess1", msg_id, "Normal plan")
        await create_task(db, pid2, "sess1", "msg", "Done")
        await update_plan_status(db, pid2, "done")
        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()

    async def test_running_plan_not_counted(self, tmp_path):
        """Only done/failed plans are checked, not running ones."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid = await create_plan(db, "sess1", msg_id, "Propose install")
        await update_plan_install_proposal(db, pid)
        await create_task(db, pid, "sess1", "msg", "Install browser?")
        # Plan still running (default status) — should not be detected
        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()

    async def test_any_language_detail_works(self, tmp_path):
        """install_proposal is language-independent — Italian detail works."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "ciao")
        pid = await create_plan(db, "sess1", msg_id, "Proponi installazione")
        await update_plan_install_proposal(db, pid)
        await create_task(db, pid, "sess1", "msg",
                          "Answer in Italian. Proponi di installare il browser.")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sess1") is True
        await db.close()


# --- M615: server-side install-proposal detection ---


from tests.conftest import make_config


@pytest.mark.asyncio
class TestRetryLoopUninstalledToolFlag:
    """M615: _retry_llm_with_validation propagates _saw_uninstalled_tool."""

    async def test_flag_set_when_validation_sees_uninstalled_tool(self):
        """If validation produces 'is not installed' error then valid plan,
        result has _saw_uninstalled_tool=True."""
        # First call: LLM returns plan with tool task → validation rejects
        bad_plan = json.dumps({
            "goal": "Navigate", "secrets": None, "extend_replan": None,
            "tasks": [{"type": "tool", "tool": "browser",
                        "detail": "go", "args": None, "expect": "page"}],
        })
        # Second call: LLM returns valid msg-only plan
        good_plan = json.dumps({
            "goal": "Ask install", "secrets": None, "extend_replan": None,
            "tasks": [{"type": "msg", "tool": None,
                        "detail": "Answer in English. Install browser?",
                        "args": None, "expect": None}],
        })
        mock_llm = AsyncMock(side_effect=[bad_plan, good_plan])
        config = make_config()
        call_count = 0

        def validate(plan):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ["Task 1: tool 'browser' is not installed. Available tools: none."]
            return []

        with patch("kiso.brain.call_llm", mock_llm):
            result = await _retry_llm_with_validation(
                config, "planner", [{"role": "user", "content": "test"}],
                {"type": "json_object"}, validate, Exception, "Plan",
            )
        assert result["_saw_uninstalled_tool"] is True

    async def test_flag_false_when_no_uninstalled_tool_errors(self):
        """Normal validation (no tool errors) → _saw_uninstalled_tool=False."""
        good_plan = json.dumps({
            "goal": "Say hello", "secrets": None, "extend_replan": None,
            "tasks": [{"type": "msg", "tool": None,
                        "detail": "Answer in English. Hello!",
                        "args": None, "expect": None}],
        })
        mock_llm = AsyncMock(return_value=good_plan)
        config = make_config()

        with patch("kiso.brain.call_llm", mock_llm):
            result = await _retry_llm_with_validation(
                config, "planner", [{"role": "user", "content": "test"}],
                {"type": "json_object"}, lambda p: [], Exception, "Plan",
            )
        assert result["_saw_uninstalled_tool"] is False

    async def test_flag_set_even_with_multiple_error_types(self):
        """If uninstalled-tool error mixed with other errors, flag still set."""
        bad = json.dumps({
            "goal": "X", "secrets": None, "extend_replan": None,
            "tasks": [{"type": "tool", "tool": "browser",
                        "detail": "go", "args": None, "expect": "page"}],
        })
        good = json.dumps({
            "goal": "Ask", "secrets": None, "extend_replan": None,
            "tasks": [{"type": "msg", "tool": None,
                        "detail": "Answer in English. Install?",
                        "args": None, "expect": None}],
        })
        mock_llm = AsyncMock(side_effect=[bad, good])
        config = make_config()
        call_count = 0

        def validate(plan):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    "Task 1: msg ordering wrong",
                    "Task 2: tool 'browser' is not installed. Available: none.",
                ]
            return []

        with patch("kiso.brain.call_llm", mock_llm):
            result = await _retry_llm_with_validation(
                config, "planner", [{"role": "user", "content": "test"}],
                {"type": "json_object"}, validate, Exception, "Plan",
            )
        assert result["_saw_uninstalled_tool"] is True


# --- M670: msg-only plan on fresh instance → install_proposal ---


def _msg_only_plan(*, needs_install=None):
    """Return a JSON string for a msg-only plan with configurable needs_install."""
    return json.dumps({
        "goal": "Ask user to install browser",
        "secrets": None,
        "extend_replan": None,
        "needs_install": needs_install,
        "tasks": [{
            "type": "msg",
            "detail": "Answer in Italian. Vuoi installare il browser?",
            "tool": None,
            "args": None,
            "expect": None,
        }],
    })


def _exec_msg_plan():
    """Return a JSON string for an exec+msg plan (not msg-only)."""
    return json.dumps({
        "goal": "Run a script",
        "secrets": None,
        "extend_replan": None,
        "needs_install": None,
        "tasks": [
            {
                "type": "exec",
                "detail": "Run hello script",
                "tool": None,
                "args": None,
                "expect": "prints hello",
            },
            {
                "type": "msg",
                "detail": "Answer in English. Report the script output and results to the user",
                "tool": None,
                "args": None,
                "expect": None,
            },
        ],
    })


@pytest.mark.asyncio
class TestM670MsgOnlyFreshInstanceProposal:
    """Install proposal must reflect explicit install intent, not missing tools."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import init_db, create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_msg_only_no_tools_rejected(self, db):
        """M1056: msg-only plan + no installed tools + needs_install=null → rejected."""
        config = make_config()
        with (
            patch("kiso.brain.call_llm", new_callable=AsyncMock,
                  return_value=_msg_only_plan(needs_install=None)),
            patch("kiso.brain.discover_tools", return_value=[]),
            pytest.raises(PlanError, match="only msg tasks"),
        ):
            await run_planner(db, config, "sess1", "admin", "vai su google.com")

    async def test_msg_only_with_tools_installed_rejected(self, db):
        """M1052: msg-only + tools installed + no needs_install → rejected."""
        config = make_config()
        fake_tool = {"name": "browser", "summary": "Web browser", "args_schema": {}}
        with (
            patch("kiso.brain.call_llm", new_callable=AsyncMock,
                  return_value=_msg_only_plan(needs_install=None)),
            patch("kiso.brain.discover_tools", return_value=[fake_tool]),
            pytest.raises(PlanError, match="only msg tasks"),
        ):
            await run_planner(db, config, "sess1", "admin", "ciao")

    async def test_msg_only_needs_install_explicit(self, db):
        """msg-only + needs_install=["browser"] → True regardless of tools."""
        config = make_config()
        with (
            patch("kiso.brain.call_llm", new_callable=AsyncMock,
                  return_value=_msg_only_plan(needs_install=["browser"])),
            patch("kiso.brain.discover_tools", return_value=[]),
        ):
            plan = await run_planner(db, config, "sess1", "admin", "vai su google.com")
        assert plan["install_proposal"] is True

    async def test_exec_msg_plan_no_tools_sets_proposal(self, db):
        """M1205d: normal action plans on fresh instances must not imply install approval."""
        config = make_config()
        with (
            patch("kiso.brain.call_llm", new_callable=AsyncMock,
                  return_value=_exec_msg_plan()),
            patch("kiso.brain.discover_tools", return_value=[]),
        ):
            plan = await run_planner(db, config, "sess1", "admin", "scrivi hello world")
        assert plan["install_proposal"] is False


# --- M897: install_proposal persistence on replan plans ---


@pytest.mark.asyncio
class TestReplanInstallProposalPersistence:
    """M897: replan plans must persist install_proposal like initial plans."""

    async def test_replan_persists_install_proposal(self, tmp_path):
        """Replan plan with install_proposal → session_has_install_proposal True."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")

        # Initial plan (failed) with install_proposal
        pid1 = await create_plan(db, "sess1", msg_id, "Exec plan")
        await update_plan_install_proposal(db, pid1)
        await update_plan_status(db, pid1, "failed")

        # Replan (child of pid1) also with install_proposal
        pid2 = await create_plan(db, "sess1", msg_id, "Replan exec",
                                 parent_id=pid1)
        await update_plan_install_proposal(db, pid2)
        await create_task(db, pid2, "sess1", "msg", "Install browser?")
        await update_plan_status(db, pid2, "done")

        assert await session_has_install_proposal(db, "sess1") is True
        await db.close()

    async def test_replan_without_proposal_overrides_parent(self, tmp_path):
        """Replan without install_proposal → False, even if parent had it."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")

        pid1 = await create_plan(db, "sess1", msg_id, "Plan with proposal")
        await update_plan_install_proposal(db, pid1)
        await update_plan_status(db, pid1, "failed")

        # Replan resolved without needing install
        pid2 = await create_plan(db, "sess1", msg_id, "Replan succeeded",
                                 parent_id=pid1)
        await create_task(db, pid2, "sess1", "msg", "Done")
        await update_plan_status(db, pid2, "done")

        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()

    async def test_multi_replan_chain_last_wins(self, tmp_path):
        """In a chain of 3 replans, the last one's install_proposal wins."""
        from kiso.store import (
            init_db, create_session, create_plan,
            update_plan_status, update_plan_install_proposal,
            session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")

        pid1 = await create_plan(db, "sess1", msg_id, "Plan 1")
        await update_plan_status(db, pid1, "failed")

        pid2 = await create_plan(db, "sess1", msg_id, "Replan 2",
                                 parent_id=pid1)
        await update_plan_install_proposal(db, pid2)
        await update_plan_status(db, pid2, "failed")

        pid3 = await create_plan(db, "sess1", msg_id, "Replan 3",
                                 parent_id=pid2)
        await update_plan_install_proposal(db, pid3)
        await update_plan_status(db, pid3, "done")

        assert await session_has_install_proposal(db, "sess1") is True

        # Plan 4 without proposal overrides the chain
        pid4 = await create_plan(db, "sess1", msg_id, "Plan 4 no proposal")
        await update_plan_status(db, pid4, "done")
        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()


# --- M901: filter needs_install against installed tools ---


@pytest.mark.asyncio
class TestM901NeedsInstallFilter:
    """M901: needs_install is filtered to remove already-installed tools."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import init_db, create_session
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_installed_tool_removed_from_needs_install(self, db):
        """needs_install: ["browser"] with browser installed → filtered out, proposal=False."""
        config = make_config()
        fake_tool = {"name": "browser", "summary": "Web browser", "args_schema": {}}
        with (
            patch("kiso.brain.call_llm", new_callable=AsyncMock,
                  return_value=_msg_only_plan(needs_install=["browser"])),
            patch("kiso.brain.discover_tools", return_value=[fake_tool]),
        ):
            plan = await run_planner(db, config, "sess1", "admin", "vai su example.com")
        assert plan["install_proposal"] is False
        assert plan["needs_install"] is None

    async def test_uninstalled_tool_preserved_in_needs_install(self, db):
        """needs_install: ["ocr"] with browser installed → ocr preserved, proposal=True."""
        config = make_config()
        fake_tool = {"name": "browser", "summary": "Web browser", "args_schema": {}}
        with (
            patch("kiso.brain.call_llm", new_callable=AsyncMock,
                  return_value=_msg_only_plan(needs_install=["ocr"])),
            patch("kiso.brain.discover_tools", return_value=[fake_tool]),
        ):
            plan = await run_planner(db, config, "sess1", "admin", "fai OCR")
        assert plan["install_proposal"] is True
        assert plan["needs_install"] == ["ocr"]

    async def test_mixed_installed_and_uninstalled_filtered(self, db):
        """needs_install: ["browser", "ocr"] with browser installed → only ocr remains."""
        config = make_config()
        fake_tool = {"name": "browser", "summary": "Web browser", "args_schema": {}}
        with (
            patch("kiso.brain.call_llm", new_callable=AsyncMock,
                  return_value=_msg_only_plan(needs_install=["browser", "ocr"])),
            patch("kiso.brain.discover_tools", return_value=[fake_tool]),
        ):
            plan = await run_planner(db, config, "sess1", "admin", "fai screenshot e OCR")
        assert plan["install_proposal"] is True
        assert plan["needs_install"] == ["ocr"]
