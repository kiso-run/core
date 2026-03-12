"""M421/M435 — Integration tests for install confirmation flow (P71, P74).

End-to-end checks that the system prevents silent skill/connector
installation without user approval.
"""

import pytest

from kiso.brain import (
    _detect_capability_gap,
    _load_modular_prompt,
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
        assert "Never install anything" in self.full
        assert "user approval" in self.full

    def test_tools_rules_msg_before_install(self):
        assert "single msg asking user to install" in self.full.lower() or \
               "single msg" in self.full.lower()

    def test_web_module_ask_before_install(self):
        assert "Single msg" in self.full or "single msg" in self.full.lower()

    def test_plugin_install_requires_prior_approval(self):
        assert "approved" in self.full.lower() or "consent" in self.full.lower()


# --- 2–4. validate_plan: install only in replan ---


class TestValidatePlanInstallConfirmation:
    """Install execs are blocked in first plan; only allowed in replan."""

    def test_install_in_first_plan_rejected(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_msg_then_install_same_plan_rejected(self):
        """msg + exec install in same first plan → rejected (user can't reply)."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. Install browser?", "expect": None},
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
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
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso connector install slack", "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
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


class TestCapabilityGapText:
    """Capability gap injection must mention asking the user."""

    def test_gap_mentions_asking_user(self):
        text = _detect_capability_gap("navigate to example.com", [])
        if text:
            assert "ask" in text.lower() or "user" in text.lower()

    def test_gap_mentions_never_install(self):
        text = _detect_capability_gap("navigate to example.com", [])
        if text:
            assert "never install" in text.lower() or "approval" in text.lower()


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

    def test_install_not_approved_blocks_first_plan(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
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
    """M428: session_has_install_proposal helper."""

    async def test_empty_session_returns_false(self, tmp_path):
        from kiso.store import init_db, create_session, session_has_install_proposal
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()

    async def test_msg_with_install_proposal_returns_true(self, tmp_path):
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, session_has_install_proposal,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        from kiso.store import save_message
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid = await create_plan(db, "sess1", msg_id, "Ask to install")
        await create_task(db, pid, "sess1", "msg",
                          "Answer in English. Would you like me to install the browser skill?")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sess1") is True
        await db.close()

    async def test_msg_without_approval_language_returns_false(self, tmp_path):
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, session_has_install_proposal,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        from kiso.store import save_message
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid = await create_plan(db, "sess1", msg_id, "Just a msg")
        await create_task(db, pid, "sess1", "msg", "Here is the result of your search.")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sess1") is False
        await db.close()


# --- M435: additional install-consent edge cases ---


@pytest.mark.asyncio
class TestM435InstallConsentEdgeCases:
    """M435: edge cases for session-aware install approval."""

    async def test_connector_install_proposal_detected(self, tmp_path):
        """'connector' keyword + approval language → True."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid = await create_plan(db, "sess1", msg_id, "Ask connector")
        await create_task(db, pid, "sess1", "msg",
                          "Would you like me to install the Slack connector?")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sess1") is True
        await db.close()

    async def test_different_session_not_counted(self, tmp_path):
        """Proposal in session A should not affect session B."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sessA")
        await create_session(db, "sessB")
        msg_id = await save_message(db, "sessA", "alice", "user", "hi")
        pid = await create_plan(db, "sessA", msg_id, "Ask install")
        await create_task(db, pid, "sessA", "msg",
                          "Shall I install the browser skill?")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sessA") is True
        assert await session_has_install_proposal(db, "sessB") is False
        await db.close()

    async def test_permission_keyword_detected(self, tmp_path):
        """'permission' approval language variant → True."""
        from kiso.store import (
            init_db, create_session, create_plan, create_task,
            update_plan_status, session_has_install_proposal, save_message,
        )
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        msg_id = await save_message(db, "sess1", "alice", "user", "hi")
        pid = await create_plan(db, "sess1", msg_id, "Ask permission")
        await create_task(db, pid, "sess1", "msg",
                          "I need your permission to install the search skill.")
        await update_plan_status(db, pid, "done")
        assert await session_has_install_proposal(db, "sess1") is True
        await db.close()
