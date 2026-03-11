"""M421 — Integration tests for install confirmation flow (P71).

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
            ["kiso_native", "skills_rules", "web", "plugin_install"],
        )

    def test_kiso_native_never_install_without_approval(self):
        assert "Never install anything" in self.full
        assert "user approval" in self.full

    def test_skills_rules_msg_before_install(self):
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
