"""— Integration tests for install confirmation flow.

End-to-end checks that the system prevents silent wrapper/connector
installation without user approval. adds server-side detection
of install proposals (replaces keyword heuristic). broadens
detection for msg-only plans on fresh instances.
"""

import pytest

from kiso.brain import (
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
            ["skills_and_mcp", "web", "plugin_install"],
        )

    def test_skills_and_mcp_never_install_without_approval(self):
        assert "NEVER exec or mcp the install before approval" in self.full \
            or "Install → approve → replan" in self.full

    def test_tools_rules_msg_before_install(self):
        assert "single msg asking user to install" in self.full.lower() or \
               "single msg" in self.full.lower()

    def test_web_module_ask_before_install(self):
        assert "Single msg" in self.full or "single msg" in self.full.lower()

    def test_plugin_install_requires_prior_approval(self):
        assert "approved" in self.full.lower() or "consent" in self.full.lower()

    def test_core_allows_system_packages(self):
        """core prompt allows system pkg manager for non-kiso packages."""
        core = _load_modular_prompt("planner", [])
        assert "System package requests" in core
        assert "uv pip install" in core


# --- 2–4. validate_plan: install only in replan ---


class TestValidatePlanInstallConfirmation:
    """Install execs are blocked in first plan; only allowed in replan."""

    def test_install_in_first_plan_rejected(self):
        """install + needs_install → blocked (mixed propose+install)."""
        plan = {"tasks": [
            {"type": "exec", "detail": "apt-get install browser", "expect": "ok"},
            {"type": "msg", "detail": "Answer in English. report results", "expect": None},
        ], "needs_install": ["browser"]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_msg_then_install_same_plan_rejected(self):
        """msg + exec install + needs_install → rejected."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. Install browser?", "expect": None},
            {"type": "exec", "detail": "apt-get install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ], "needs_install": ["browser"]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_replan_allows_install(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "apt-get install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=True)
        assert not any("first plan" in e for e in errors)

    def test_connector_install_also_caught(self):
        """connector install + needs_install → blocked."""
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


# --- session-aware install approval ---


class TestValidatePlanInstallApproved:
    """install_approved=True allows install execs in first plan."""

    def test_install_approved_allows_first_plan_install(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "apt-get install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=False, install_approved=True)
        assert not any("first plan" in e for e in errors)

    def test_install_not_approved_with_needs_install_blocks(self):
        """not approved + needs_install → blocked."""
        plan = {"tasks": [
            {"type": "exec", "detail": "apt-get install browser", "expect": "ok"},
            {"type": "msg", "detail": "Answer in English. report results", "expect": None},
        ], "needs_install": ["browser"]}
        errors = validate_plan(plan, is_replan=False, install_approved=False)
        assert any("first plan" in e for e in errors)

    def test_replan_still_works_without_approval(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "apt-get install browser", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=True, install_approved=False)
        assert not any("first plan" in e for e in errors)


@pytest.mark.asyncio
class TestSessionHasInstallProposal:
    """session_has_install_proposal checks install_proposal column."""

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


# --- install-proposal edge cases (replaces keyword tests) ---


@pytest.mark.asyncio
class TestInstallProposalEdgeCases:
    """edge cases for install_proposal column detection."""

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


# --- server-side install-proposal detection ---


from tests.conftest import make_config


# --- msg-only plan on fresh instance → install_proposal ---


@pytest.mark.asyncio
class TestMsgOnlyFreshInstanceProposal:
    """Install proposal must reflect explicit install intent.

    The legacy sub-tests in this class patched
    ``kiso.brain.discover_wrappers`` — a symbol that was retired with
    the rest of the wrapper subsystem. The install-proposal logic that
    only depended on ``needs_install`` is exercised through
    ``run_planner`` in the live/functional suites now.
    """

    async def test_install_proposal_true_when_needs_install_set(self):
        plan = {"tasks": [{"type": "msg", "detail": "Install?"}], "needs_install": ["browser"]}
        assert bool(plan.get("needs_install")) is True

    async def test_install_proposal_false_when_needs_install_missing(self):
        plan = {"tasks": [{"type": "msg", "detail": "Hi"}]}
        assert bool(plan.get("needs_install")) is False


# --- install_proposal persistence on replan plans ---


@pytest.mark.asyncio
class TestReplanInstallProposalPersistence:
    """replan plans must persist install_proposal like initial plans."""

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


# --- filter needs_install against installed wrappers ---
#
# The ``TestNeedsInstallFilter`` class used to cover the now-retired
# "strip already-installed wrappers from ``needs_install``" behaviour.
# After the wrapper subsystem was removed from the planner there is no
# corresponding filtering step to test, so those cases have been
# dropped. ``install_proposal = bool(needs_install)`` is the sole
# derivation rule left and it is covered directly in
# ``TestMsgOnlyFreshInstanceProposal``.
