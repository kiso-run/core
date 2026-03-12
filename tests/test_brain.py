"""Tests for kiso/brain.py — planner brain."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema as _jsonschema
import pytest

import aiosqlite
from kiso.brain import (
    BRIEFER_MODULES,
    BRIEFER_SCHEMA,
    BrieferError,
    ClassifierError,
    CURATOR_SCHEMA,
    CuratorError,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    ExecTranslatorError,
    MessengerError,
    ParaphraserError,
    PlanError,
    ReviewError,
    SummarizerError,
    _load_modular_prompt,
    _load_system_prompt,
    _prompt_cache,
    _ROLES_DIR,
    invalidate_prompt_cache,
    _strip_fences,
    _repair_json,
    _is_plugin_discovery_search,
    build_briefer_messages,
    build_classifier_messages,
    build_curator_messages,
    build_exec_translator_messages,
    build_messenger_messages,
    build_paraphraser_messages,
    build_planner_messages,
    build_reviewer_messages,
    build_summarizer_messages,
    classify_message,
    classify_inflight,
    build_inflight_classifier_messages,
    CLASSIFIER_CATEGORIES,
    INFLIGHT_CATEGORIES,
    is_stop_message,
    _sanitize_messenger_output,
    run_briefer,
    run_curator,
    run_exec_translator,
    run_fact_consolidation,
    run_messenger,
    run_paraphraser,
    run_planner,
    run_reviewer,
    run_summarizer,
    validate_briefing,
    validate_curator,
    validate_plan,
    validate_review,
    REVIEW_STATUSES,
    REVIEW_STATUS_STUCK,
    _retry_llm_with_validation,
)
from kiso.config import Config, Provider, KISO_DIR, SETTINGS_DEFAULTS, MODEL_DEFAULTS


def _full_settings(**overrides) -> dict:
    """Return a complete settings dict (all required keys) with optional overrides.

    Briefer is disabled by default in tests to avoid interfering with
    mocked call_llm. Tests that need the briefer should pass
    ``briefer_enabled=True`` explicitly.
    """
    return {**SETTINGS_DEFAULTS, "briefer_enabled": False, **overrides}


def _full_models(**overrides) -> dict:
    """Return a complete models dict with optional overrides."""
    return {**MODEL_DEFAULTS, **overrides}
from kiso.llm import LLMError
from kiso.store import (
    create_session,
    init_db,
    save_fact,
    save_fact_tags,
    save_message,
)


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """Ensure the prompt cache is clean before and after every test."""
    invalidate_prompt_cache()
    yield
    invalidate_prompt_cache()


# --- Worker phase constants (M109c) ---


def test_worker_phases_frozenset():
    """WORKER_PHASES contains all four phase constants."""
    from kiso.brain import (
        WORKER_PHASE_CLASSIFYING,
        WORKER_PHASE_EXECUTING,
        WORKER_PHASE_IDLE,
        WORKER_PHASE_PLANNING,
        WORKER_PHASES,
    )
    assert WORKER_PHASES == frozenset({
        WORKER_PHASE_CLASSIFYING, WORKER_PHASE_PLANNING,
        WORKER_PHASE_EXECUTING, WORKER_PHASE_IDLE,
    })
    assert len(WORKER_PHASES) == 4


# --- M111d: reviewer prompt hardening ---


def test_reviewer_prompt_contains_empty_output_guard():
    """M111d: reviewer.md must instruct LLM to set learn=null on empty output."""
    prompt = _load_system_prompt("reviewer")
    assert "output empty" in prompt or "empty/whitespace" in prompt or "empty or whitespace" in prompt
    assert "Null if nothing useful" in prompt or "learn MUST be null" in prompt


def test_reviewer_prompt_contains_reason_required_rule():
    """M111d: reviewer.md must require reason for replan status."""
    prompt = _load_system_prompt("reviewer")
    assert ("required (non-null, non-empty" in prompt or "required when replan or stuck" in prompt) and "replan" in prompt


# --- M318: reviewer learn quality rules ---


def test_reviewer_prompt_max_3_learns():
    """M318: reviewer learn cap reduced from 5 to 3."""
    prompt = _load_system_prompt("reviewer")
    assert "max 3" in prompt


def test_reviewer_prompt_self_contained_rule():
    """M318/M425: learnings must be self-contained with subject context."""
    prompt = _load_system_prompt("reviewer")
    assert "self-contained with subject" in prompt


def test_reviewer_prompt_consolidation_rule():
    """M318/M425: learnings must consolidate related observations."""
    prompt = _load_system_prompt("reviewer")
    assert "consolidate" in prompt.lower()


def test_reviewer_prompt_transient_rule():
    """M318/M425: learnings must not include transient data."""
    prompt = _load_system_prompt("reviewer")
    assert "transient" in prompt.lower()
    assert "element indices" in prompt.lower()


# --- M320: clean_learn_items ---


class TestCleanLearnItems:
    def test_filters_short_items(self):
        from kiso.brain import clean_learn_items
        result = clean_learn_items(["too short", "This is a valid learning about guidance.studio"])
        assert result == ["This is a valid learning about guidance.studio"]

    def test_filters_transient_installed(self):
        from kiso.brain import clean_learn_items
        result = clean_learn_items(["browser skill installed successfully"])
        assert result == []

    def test_filters_transient_loaded(self):
        from kiso.brain import clean_learn_items
        result = clean_learn_items(["guidance.studio homepage loaded successfully"])
        assert result == []

    def test_filters_ephemeral_indices(self):
        from kiso.brain import clean_learn_items
        result = clean_learn_items([
            "The contact form includes Name [8], Email [9], and details [10]."
        ])
        assert result == []

    def test_preserves_valid_learnings(self):
        from kiso.brain import clean_learn_items
        items = [
            "guidance.studio has a contact form at /venture-launchpad",
            "Python project uses pytest for testing",
        ]
        result = clean_learn_items(items)
        assert result == items

    def test_boundary_exactly_15_chars(self):
        from kiso.brain import clean_learn_items
        item = "exactly15chars!!"  # 16 chars
        assert len(item) >= 15
        result = clean_learn_items([item])
        assert result == [item]

    def test_single_index_preserved(self):
        """A single [N] is fine — only 2+ triggers the filter."""
        from kiso.brain import clean_learn_items
        result = clean_learn_items(["guidance.studio uses port [443] for HTTPS"])
        assert len(result) == 1

    def test_empty_list(self):
        from kiso.brain import clean_learn_items
        assert clean_learn_items([]) == []

    def test_ran_successfully_filtered(self):
        from kiso.brain import clean_learn_items
        result = clean_learn_items(["the test suite ran successfully on the project"])
        assert result == []


# --- M373: output-backed learning validation ---

class TestLearningContradictsOutput:
    def test_negative_claim_contradicted_by_output(self):
        from kiso.brain import clean_learn_items
        items = ["kernel release not stated in the output"]
        output = "Linux 6.1.0-20-amd64 kernel release info"
        result = clean_learn_items(items, task_output=output)
        assert result == []

    def test_negative_claim_not_contradicted(self):
        from kiso.brain import clean_learn_items
        items = ["ssh key not found on this system"]
        output = "total 0\nno files here"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1

    def test_normal_learning_preserved(self):
        from kiso.brain import clean_learn_items
        items = ["Project uses Flask framework for web serving"]
        output = "Flask==2.3.0 installed"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1

    def test_no_output_skips_check(self):
        from kiso.brain import clean_learn_items
        items = ["docker version not available on this host"]
        result = clean_learn_items(items, task_output=None)
        assert len(result) == 1

    def test_not_installed_contradicted(self):
        from kiso.brain import clean_learn_items
        items = ["python package not installed on the system"]
        output = "python3 3.11.2 is installed at /usr/bin/python3"
        result = clean_learn_items(items, task_output=output)
        assert result == []


# --- validate_plan ---

class TestValidatePlan:
    def test_valid_plan(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "files listed", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_empty_tasks(self):
        errors = validate_plan({"tasks": []})
        assert any("not be empty" in e for e in errors)

    def test_missing_tasks_key(self):
        errors = validate_plan({})
        assert any("not be empty" in e for e in errors)

    def test_exec_without_expect(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": None},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("exec task must have a non-null expect" in e for e in errors)

    def test_skill_without_expect(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": None, "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("tool task must have a non-null expect" in e for e in errors)

    def test_msg_with_expect(self):
        plan = {"tasks": [
            {"type": "msg", "detail": "done", "expect": "something"},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must have expect = null" in e for e in errors)

    def test_msg_with_non_null_skill(self):
        """M84i: msg task with skill != null must fail validation."""
        plan = {"tasks": [
            {"type": "msg", "detail": "done", "expect": None, "skill": "my-skill", "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must have skill = null" in e for e in errors)

    def test_msg_with_non_null_args(self):
        """M84i: msg task with args != null must fail validation."""
        plan = {"tasks": [
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": '{"key": "val"}'},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must have args = null" in e for e in errors)

    def test_m386_msg_detail_only_language_prefix_fails(self):
        """M386: msg detail with only language prefix is rejected."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in Italian.", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("empty after language prefix" in e for e in errors)

    def test_m386_msg_detail_with_content_after_prefix_passes(self):
        """M386: msg detail with substantive content after prefix passes."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in Italian. Tell user the SSH key is at ~/.kiso/sys/ssh/",
             "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("empty after language prefix" in e for e in errors)

    def test_m386_msg_detail_without_prefix_passes(self):
        """M386: msg detail without language prefix passes (no extra validation)."""
        plan = {"tasks": [
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("empty after language prefix" in e for e in errors)

    def test_last_task_not_msg(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "ok"},
        ]}
        errors = validate_plan(plan)
        assert any("Last task must be type 'msg' or 'replan'" in e for e in errors)

    def test_multiple_errors(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": None},
            {"type": "exec", "detail": "pwd", "expect": None},
        ]}
        errors = validate_plan(plan)
        # Two exec-without-expect + last-not-msg
        assert len(errors) >= 3

    def test_single_msg_task_valid(self):
        plan = {"tasks": [
            {"type": "msg", "detail": "Hello!", "expect": None},
        ]}
        assert validate_plan(plan) == []

    def test_exec_with_expect_valid(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "echo hi", "expect": "prints hi"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        assert validate_plan(plan) == []

    # --- M7: skill validation in validate_plan ---

    def test_skill_name_required(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "do thing", "expect": "ok", "skill": None, "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("tool task must have a non-null tool name" in e for e in errors)

    def test_skill_not_installed(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=["echo"])
        assert any("tool 'search' is not installed" in e for e in errors)
        assert any("Available tools: echo" in e for e in errors)

    def test_skill_not_installed_suggests_asking_user(self):
        """M418/M419: validation error guides LLM to ask user, end plan with msg."""
        plan = {"tasks": [
            {"type": "skill", "detail": "browse", "expect": "ok", "skill": "browser", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=[])
        assert any("CANNOT use 'browser'" in e for e in errors)
        assert any("SINGLE msg task" in e for e in errors)
        assert any("offer alternatives" in e for e in errors)
        assert any("End the plan" in e for e in errors)

    def test_skill_not_installed_empty_list(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=[])
        assert any("Available tools: none" in e for e in errors)

    def test_skill_installed_passes(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=["search"])
        assert errors == []

    def test_skill_no_installed_list_skips_check(self):
        """When installed_skills is None, skip skill-not-installed check."""
        plan = {"tasks": [
            {"type": "skill", "detail": "search", "expect": "ok", "skill": "search", "args": "{}"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan, installed_skills=None)
        assert errors == []

    def test_unknown_task_type_rejected(self):
        """Plan with type='query' should produce an error."""
        plan = {"tasks": [
            {"type": "query", "detail": "search", "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("unknown type" in e for e in errors)

    def test_none_task_type_rejected(self):
        """Plan with type=None should produce an error."""
        plan = {"tasks": [
            {"detail": "search", "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("unknown type" in e for e in errors)

    def test_plan_too_many_tasks_rejected(self):
        """Plan with 25 tasks, max_tasks=20, should produce an error."""
        tasks = [
            {"type": "exec", "detail": f"cmd-{i}", "expect": "ok"}
            for i in range(24)
        ] + [{"type": "msg", "detail": "done", "expect": None}]
        plan = {"tasks": tasks}
        errors = validate_plan(plan, max_tasks=20)
        assert any("max allowed is 20" in e for e in errors)

    def test_plan_exactly_at_max_tasks_accepted(self):
        """Plan with exactly max_tasks tasks should pass."""
        tasks = [
            {"type": "exec", "detail": f"cmd-{i}", "expect": "ok"}
            for i in range(19)
        ] + [{"type": "msg", "detail": "done", "expect": None}]
        plan = {"tasks": tasks}
        errors = validate_plan(plan, max_tasks=20)
        assert not any("max allowed" in e for e in errors)

    # --- M137: msg must come after data-gathering tasks ---

    def test_msg_before_exec_rejected(self):
        """M137: msg task before exec tasks must fail validation."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in Italian. describe results", "expect": None, "skill": None, "args": None},
            {"type": "exec", "detail": "curl site", "expect": "HTML fetched"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must come after" in e for e in errors)

    def test_msg_before_search_rejected(self):
        """M137: msg before search is also rejected."""
        plan = {"tasks": [
            {"type": "msg", "detail": "let me check", "expect": None, "skill": None, "args": None},
            {"type": "search", "detail": "query", "expect": "results"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("msg task must come after" in e for e in errors)

    def test_msg_after_all_exec_valid(self):
        """M137: msg after all exec/search tasks is valid."""
        plan = {"tasks": [
            {"type": "exec", "detail": "curl site", "expect": "HTML fetched"},
            {"type": "exec", "detail": "grep title", "expect": "title found"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("msg task must come after" in e for e in errors)

    def test_msg_only_plan_valid(self):
        """M137: plan with only a msg (no data tasks) is valid."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Hello!", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_msg_between_exec_and_replan_valid(self):
        """M137: [exec, msg, replan] — msg after exec, before replan — valid."""
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "files"},
            {"type": "msg", "detail": "progress update", "expect": None, "skill": None, "args": None},
            {"type": "replan", "detail": "decide next", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("msg task must come after" in e for e in errors)

    # --- M25: replan task type ---

    def test_replan_as_last_task_valid(self):
        """Plan with exec + replan → valid."""
        plan = {"tasks": [
            {"type": "exec", "detail": "read registry", "expect": "JSON output", "skill": None, "args": None},
            {"type": "replan", "detail": "install appropriate skill", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_replan_not_last_task_invalid(self):
        """Replan followed by msg → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": None, "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("replan task can only be the last task" in e for e in errors)

    def test_replan_with_expect_invalid(self):
        """Replan task with non-null expect → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": "something", "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("replan task must have expect = null" in e for e in errors)

    def test_replan_with_skill_invalid(self):
        """Replan task with non-null skill → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": None, "skill": "search", "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("replan task must have skill = null" in e for e in errors)

    def test_replan_with_args_invalid(self):
        """Replan task with non-null args → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate", "expect": None, "skill": None, "args": "{}"},
        ]}
        errors = validate_plan(plan)
        assert any("replan task must have args = null" in e for e in errors)

    def test_multiple_replan_tasks_invalid(self):
        """Two replan tasks → invalid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "first", "expect": None, "skill": None, "args": None},
            {"type": "replan", "detail": "second", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("at most one replan task" in e for e in errors)

    def test_replan_only_plan_valid(self):
        """Plan with only a replan task → valid."""
        plan = {"tasks": [
            {"type": "replan", "detail": "investigate first", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_extend_replan_field_accepted(self):
        """Plan with extend_replan=2 → valid (extend_replan is a plan-level field, not validated in validate_plan)."""
        plan = {
            "extend_replan": 2,
            "tasks": [
                {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
            ],
        }
        assert validate_plan(plan) == []

    def test_last_task_must_be_msg_or_replan(self):
        """Plan ending with exec (not msg or replan) → invalid."""
        plan = {"tasks": [
            {"type": "exec", "detail": "ls", "expect": "ok"},
        ]}
        errors = validate_plan(plan)
        assert any("Last task must be type 'msg' or 'replan'" in e for e in errors)

    # --- M31: search task type ---

    def test_search_task_valid(self):
        """search + expect, skill=null → valid."""
        plan = {"tasks": [
            {"type": "search", "detail": "best restaurants in Milan", "expect": "list of restaurants", "skill": None, "args": None},
            {"type": "msg", "detail": "present results", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_search_task_with_args(self):
        """search + args JSON string → valid."""
        plan = {"tasks": [
            {"type": "search", "detail": "best SEO agencies", "expect": "list of agencies", "skill": None, "args": '{"max_results": 10, "lang": "it", "country": "IT"}'},
            {"type": "msg", "detail": "present results", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_search_task_missing_expect(self):
        """search without expect → error."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": None, "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search task must have a non-null expect" in e for e in errors)

    def test_search_task_with_skill(self):
        """search with skill set → error."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": "search", "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search task must have skill = null" in e for e in errors)

    def test_search_task_not_last(self):
        """Plan ending with search → error (last must be msg or replan)."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("Last task must be type 'msg' or 'replan'" in e for e in errors)

    def test_plan_search_then_msg(self):
        """search + msg → valid."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": None, "args": None},
            {"type": "msg", "detail": "present results", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    def test_plan_search_then_replan(self):
        """search + replan → valid (investigation pattern)."""
        plan = {"tasks": [
            {"type": "search", "detail": "find info", "expect": "results", "skill": None, "args": None},
            {"type": "replan", "detail": "plan next steps", "expect": None, "skill": None, "args": None},
        ]}
        assert validate_plan(plan) == []

    # --- M420: install only allowed in replan ---

    def test_m420_install_in_first_plan_rejected(self):
        """exec install in first plan (is_replan=False) → error."""
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "installed"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_m420_msg_then_install_still_rejected(self):
        """msg + exec install in same first plan → still rejected (user can't reply)."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. Confirm install", "expect": None},
            {"type": "exec", "detail": "kiso skill install browser", "expect": "installed"},
            {"type": "replan", "detail": "continue after install", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_m420_msg_only_no_install_accepted(self):
        """msg asking about install (no exec install) → passes."""
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. Ask to install browser skill", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert not any("first plan" in e for e in errors)

    def test_m420_replan_allows_install(self):
        """is_replan=True allows exec install (user approved in prior cycle)."""
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "installed"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=True)
        assert not any("first plan" in e for e in errors)

    def test_m420_multiple_installs_single_error(self):
        """Multiple install execs → only one error (first install)."""
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso skill install browser", "expect": "installed"},
            {"type": "exec", "detail": "kiso connector install slack", "expect": "installed"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        install_errors = [e for e in errors if "first plan" in e]
        assert len(install_errors) == 1
        assert "Task 1:" in install_errors[0]

    def test_m420_connector_install_also_caught(self):
        """kiso connector install also blocked in first plan."""
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso connector install telegram", "expect": "installed"},
            {"type": "msg", "detail": "done", "expect": None},
        ]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)


# --- _load_system_prompt ---

class TestLoadSystemPrompt:
    def test_package_default_when_no_user_file(self):
        prompt = _load_system_prompt("planner")
        assert "planner" in prompt

    def test_user_override_takes_priority(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "planner.md").write_text("Custom prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("planner")
        assert prompt == "Custom prompt"

    def test_unknown_role_raises(self, tmp_path):
        # Use a tmp_path with no roles dir as KISO_DIR to avoid class-wide mock
        with patch("kiso.brain.KISO_DIR", tmp_path):
            with pytest.raises(FileNotFoundError, match="No prompt found for role 'nonexistent'"):
                _load_system_prompt("nonexistent")


# --- _load_system_prompt — cache (M65b) ---

class TestLoadSystemPromptCache:
    def test_result_is_cached_on_second_call(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        role_file = roles_dir / "planner.md"
        role_file.write_text("v1")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            first = _load_system_prompt("planner")
        # Overwrite file — cached value should still be returned
        role_file.write_text("v2")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            second = _load_system_prompt("planner")
        assert first == "v1"
        assert second == "v1"  # still cached

    def test_invalidate_prompt_cache_clears_cache(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        role_file = roles_dir / "planner.md"
        role_file.write_text("v1")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            _load_system_prompt("planner")
        assert "planner" in _prompt_cache
        invalidate_prompt_cache()
        assert "planner" not in _prompt_cache

    def test_after_invalidation_file_is_reread(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        role_file = roles_dir / "planner.md"
        role_file.write_text("v1")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            _load_system_prompt("planner")
        role_file.write_text("v2")
        invalidate_prompt_cache()
        with patch("kiso.brain.KISO_DIR", tmp_path):
            result = _load_system_prompt("planner")
        assert result == "v2"


# --- build_planner_messages ---

class TestBuildPlannerMessages:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(context_messages=3),
            raw={},
        )

    async def test_returns_3_tuple(self, db, config):
        """M208: build_planner_messages returns (messages, names, info)."""
        await create_session(db, "sess1")
        result = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert len(result) == 3
        msgs, names, info = result
        assert isinstance(msgs, list)
        assert isinstance(names, list)
        assert isinstance(info, list)

    async def test_basic_no_context(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "## Caller Role\nadmin" in msgs[1]["content"]
        assert "## New Message" in msgs[1]["content"]
        assert "hello" in msgs[1]["content"]
        assert "<<<USER_MSG_" in msgs[1]["content"]

    async def test_includes_summary(self, db, config):
        await create_session(db, "sess1")
        await db.execute("UPDATE sessions SET summary = 'previous context' WHERE session = 'sess1'")
        await db.commit()
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Session Summary" in msgs[1]["content"]
        assert "previous context" in msgs[1]["content"]

    async def test_includes_facts(self, db, config):
        await create_session(db, "sess1")
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Python 3.12", "curator"))
        await db.commit()
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Known Facts" in msgs[1]["content"]
        assert "Python 3.12" in msgs[1]["content"]

    async def test_facts_grouped_by_category(self, db, config):
        """Facts are grouped by category in planner context."""
        await create_session(db, "sess1")
        from kiso.store import save_fact
        await save_fact(db, "Uses Flask", "curator", category="project")
        await save_fact(db, "Prefers dark mode", "curator", category="user")
        await save_fact(db, "Git available", "curator", category="tool")
        await save_fact(db, "Some general fact", "curator", category="general")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "### Project" in content
        assert "### User" in content
        assert "### Tool" in content
        assert "### General" in content
        # Verify order: project before user before tool before general
        proj_pos = content.index("### Project")
        user_pos = content.index("### User")
        tool_pos = content.index("### Tool")
        gen_pos = content.index("### General")
        assert proj_pos < user_pos < tool_pos < gen_pos

    async def test_admin_facts_hierarchy(self, db, config):
        """M44f: admin context shows current-session+global facts in ## Known Facts (primary)
        and other-session facts in ## Context from Other Sessions (background)."""
        from kiso.store import save_fact
        await create_session(db, "sess1")
        await create_session(db, "sess-other")
        await save_fact(db, "Alice prefers verbose", "curator", session="sess1", category="user")
        await save_fact(db, "Bob prefers brief", "curator", session="sess-other", category="user")
        await save_fact(db, "Uses Docker", "curator")  # no session — global
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        # Current session + global go into primary block, no session label
        assert "## Known Facts" in content
        assert "Alice prefers verbose" in content
        assert "Uses Docker" in content
        known_pos = content.index("## Known Facts")
        assert "Alice prefers verbose" in content[known_pos:]
        assert "Uses Docker" in content[known_pos:]
        # Other sessions go into secondary block, with session label
        assert "## Context from Other Sessions" in content
        other_pos = content.index("## Context from Other Sessions")
        assert "Bob prefers brief [session:sess-other]" in content[other_pos:]
        # Current-session fact must NOT carry a session label
        assert "Alice prefers verbose [session:" not in content

    async def test_non_admin_facts_no_session_labels(self, db, config):
        """M44f: non-admin planner context never shows session labels or the other-sessions block."""
        from kiso.store import save_fact
        await create_session(db, "sess1")
        await save_fact(db, "Uses pytest", "curator", session="sess1", category="tool")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "user", "hello")
        content = msgs[1]["content"]
        assert "Uses pytest" in content
        assert "[session:" not in content
        assert "## Context from Other Sessions" not in content

    async def test_unknown_category_falls_back_to_general(self, db, config):
        """Facts with unknown categories appear under ### General."""
        await create_session(db, "sess1")
        from kiso.store import save_fact
        await save_fact(db, "Exotic fact", "curator", category="exotic")
        await save_fact(db, "Normal fact", "curator", category="general")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "### General" in content
        assert "Exotic fact" in content
        # Unknown category should NOT get its own heading
        assert "### Exotic" not in content

    async def test_includes_pending(self, db, config):
        await create_session(db, "sess1")
        await db.execute(
            "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
            ("Which DB?", "sess1", "curator"),
        )
        await db.commit()
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert "## Pending Questions" in msgs[1]["content"]
        assert "Which DB?" in msgs[1]["content"]

    async def test_includes_recent_messages(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "first msg")
        await save_message(db, "sess1", "alice", "user", "second msg")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "third")
        assert "## Recent Messages" in msgs[1]["content"]
        assert "first msg" in msgs[1]["content"]
        assert "second msg" in msgs[1]["content"]

    async def test_respects_context_limit(self, db, config):
        """Only last context_messages (3) messages are included."""
        await create_session(db, "sess1")
        for i in range(5):
            await save_message(db, "sess1", "alice", "user", f"msg-{i}")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "new")
        content = msgs[1]["content"]
        # Only last 3 should be present
        assert "msg-0" not in content
        assert "msg-1" not in content
        assert "msg-2" in content
        assert "msg-3" in content
        assert "msg-4" in content

    async def test_excludes_untrusted_from_recent(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "trusted", "user", "good msg", trusted=True)
        await save_message(db, "sess1", "stranger", "user", "bad msg", trusted=False, processed=True)
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "new")
        content = msgs[1]["content"]
        assert "good msg" in content
        assert "bad msg" not in content

    async def test_no_session_doesnt_crash(self, db, config):
        """Building context for a nonexistent session should not crash."""
        msgs, _installed, *_ = await build_planner_messages(db, config, "nonexistent", "admin", "hello")
        assert len(msgs) == 2

    # --- M7: skills in planner context ---

    async def test_includes_skills_when_present(self, db, config):
        await create_session(db, "sess1")
        fake_skills = [
            {"name": "search", "summary": "Web search", "args_schema": {
                "query": {"type": "string", "required": True, "description": "search query"},
            }, "env": {}, "session_secrets": [], "path": "/fake", "version": "0.1.0", "description": ""},
        ]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "search for X")
        content = msgs[1]["content"]
        assert "## Tools" in content
        assert "search — Web search" in content
        assert "query (string, required): search query" in content

    # --- System environment in planner context ---

    async def test_includes_system_environment(self, db, config):
        """Planner context includes ## System Environment with OS and binaries."""
        await create_session(db, "sess1")
        from kiso.config import KISO_DIR
        fake_env = {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.1.0"},
            "shell": "/bin/sh",
            "exec_cwd": str(KISO_DIR / "sessions"),
            "exec_env": "PATH (sys/bin prepended) + HOME + git/ssh env vars when config exists",
            "llm_timeout": 120,
            "max_output_size": 1_048_576,
            "available_binaries": ["git", "python3"],
            "missing_binaries": ["docker"],
            "connectors": [],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
            "sys_bin_path": str(KISO_DIR / "sys" / "bin"),
            "reference_docs_path": str(KISO_DIR / "reference"),
            "registry_url": "https://raw.githubusercontent.com/kiso-run/core/main/registry.json",
        }
        with patch("kiso.brain.get_system_env", return_value=fake_env):
            msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "## System Environment" in content
        assert "Linux x86_64" in content
        assert "git, python3" in content
        # Session name should be included in the system env section
        assert "Session: sess1" in content
        expected_cwd = str(KISO_DIR / "sessions" / "sess1")
        assert f"Exec CWD: {expected_cwd}" in content

    async def test_system_env_after_facts_before_pending(self, db, config):
        """System Environment section appears between Known Facts and Pending Questions."""
        await create_session(db, "sess1")
        await db.execute("INSERT INTO facts (content, source) VALUES (?, ?)", ("Python 3.12", "curator"))
        await db.execute(
            "INSERT INTO pending (content, scope, source) VALUES (?, ?, ?)",
            ("Which DB?", "sess1", "curator"),
        )
        await db.commit()
        from kiso.config import KISO_DIR
        fake_env = {
            "os": {"system": "Linux", "machine": "x86_64", "release": "6.1.0"},
            "shell": "/bin/sh",
            "exec_cwd": str(KISO_DIR / "sessions"),
            "exec_env": "PATH (sys/bin prepended) + HOME + git/ssh env vars when config exists",
            "llm_timeout": 120,
            "max_output_size": 1_048_576,
            "available_binaries": ["git"],
            "missing_binaries": [],
            "connectors": [],
            "max_plan_tasks": 20,
            "max_replan_depth": 3,
            "sys_bin_path": str(KISO_DIR / "sys" / "bin"),
            "reference_docs_path": str(KISO_DIR / "reference"),
            "registry_url": "https://raw.githubusercontent.com/kiso-run/core/main/registry.json",
        }
        with patch("kiso.brain.get_system_env", return_value=fake_env):
            msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        facts_pos = content.index("## Known Facts")
        sysenv_pos = content.index("## System Environment")
        pending_pos = content.index("## Pending Questions")
        assert facts_pos < sysenv_pos < pending_pos

    async def test_no_skills_section_when_empty(self, db, config):
        await create_session(db, "sess1")
        with patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "## Skills" not in content

    async def test_safety_facts_injected(self, db, config):
        """M411: safety facts appear in planner messages as ## Safety Rules."""
        from kiso.store import save_fact
        await create_session(db, "sess1")
        await save_fact(db, "Never delete /data without confirmation", "admin",
                        category="safety")
        await save_fact(db, "Production DB is read-only", "admin",
                        category="safety")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "## Safety Rules (MUST OBEY)" in content
        assert "Never delete /data" in content
        assert "Production DB is read-only" in content

    async def test_no_safety_section_when_empty(self, db, config):
        """M411: no safety section when no safety facts exist."""
        await create_session(db, "sess1")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "Safety Rules" not in content

    async def test_user_tools_filtered(self, db, config):
        await create_session(db, "sess1")
        fake_skills = [
            {"name": "search", "summary": "Search", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake", "version": "0.1.0", "description": ""},
            {"name": "aider", "summary": "Code edit", "args_schema": {},
             "env": {}, "session_secrets": [], "path": "/fake2", "version": "0.1.0", "description": ""},
        ]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, _installed, *_ = await build_planner_messages(
                db, config, "sess1", "user", "hello", user_tools=["search"],
            )
        content = msgs[1]["content"]
        # Tools section should only show search, not aider (restricted user)
        tools_start = content.find("## Tools")
        skills_section = content[tools_start:tools_start + 500] if tools_start >= 0 else ""
        assert "search" in skills_section
        assert "aider" not in skills_section

    async def test_logs_warning_when_no_skills(self, db, config, caplog):
        """M3: build_planner_messages logs warning when discover_tools returns empty."""
        import logging
        await create_session(db, "sess1")
        with (
            patch("kiso.brain.discover_tools", return_value=[]),
            caplog.at_level(logging.WARNING, logger="kiso.brain"),
        ):
            msgs, names, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        assert names == []
        assert "discover_tools() returned empty" in caplog.text


# --- run_planner ---

VALID_PLAN = json.dumps({
    "goal": "Say hello",
    "secrets": None,
    "tasks": [{"type": "msg", "detail": "Hello!", "skill": None, "args": None, "expect": None}],
})

INVALID_PLAN = json.dumps({
    "goal": "Bad plan",
    "secrets": None,
    "tasks": [{"type": "exec", "detail": "ls", "skill": None, "args": None, "expect": None}],
})


class TestRunPlanner:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3, context_messages=5),
            raw={},
        )

    async def test_valid_plan_first_try(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_PLAN):
            plan = await run_planner(db, config, "sess1", "admin", "hello")
        assert plan["goal"] == "Say hello"
        assert len(plan["tasks"]) == 1

    async def test_retry_on_invalid_then_valid(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=[INVALID_PLAN, VALID_PLAN]):
            plan = await run_planner(db, config, "sess1", "admin", "hello")
        assert plan["goal"] == "Say hello"

    async def test_all_retries_exhausted(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=INVALID_PLAN):
            with pytest.raises(PlanError, match="validation failed after 3"):
                await run_planner(db, config, "sess1", "admin", "hello")

    async def test_llm_error_raises_plan_error(self, db, config):
        from kiso.llm import LLMError
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(PlanError, match="LLM call failed.*API down"):
                await run_planner(db, config, "sess1", "admin", "hello")

    async def test_invalid_json_raises_plan_error(self, db, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json at all"):
            with pytest.raises(PlanError, match="(?i)invalid JSON"):
                await run_planner(db, config, "sess1", "admin", "hello")

    async def test_invalid_json_retries_before_raising(self, db, config):
        """M84b: JSON parse error should retry, not raise immediately."""
        call_count = 0

        async def _bad_then_good(cfg, role, messages, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "not json at all"
            return VALID_PLAN

        with patch("kiso.brain.call_llm", side_effect=_bad_then_good):
            plan = await run_planner(db, config, "sess1", "admin", "hello")

        assert call_count == 2, "Expected retry after JSON error"
        assert plan["goal"] == "Say hello"

    async def test_retry_appends_error_feedback(self, db, config):
        """On retry, error feedback and previous assistant response are appended."""
        call_messages = []

        async def _capture_call(cfg, role, messages, **kw):
            call_messages.append(len(messages))
            if len(call_messages) == 1:
                return INVALID_PLAN
            return VALID_PLAN

        with patch("kiso.brain.call_llm", side_effect=_capture_call):
            await run_planner(db, config, "sess1", "admin", "hello")

        # First call: system + user = 2 messages
        assert call_messages[0] == 2
        # Second call: +assistant (bad plan) +user (error feedback) = 4
        assert call_messages[1] == 4


# --- _load_system_prompt (reviewer) ---

class TestLoadSystemPromptReviewer:
    def test_reviewer_default_when_no_file(self):
        prompt = _load_system_prompt("reviewer")
        assert "task reviewer" in prompt

    def test_reviewer_reads_file_when_exists(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "reviewer.md").write_text("Custom reviewer prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("reviewer")
        assert prompt == "Custom reviewer prompt"


# --- validate_review ---

class TestValidateReview:
    def test_ok_valid(self):
        review = {"status": "ok", "reason": None, "learn": None}
        assert validate_review(review) == []

    def test_ok_with_learn(self):
        review = {"status": "ok", "reason": None, "learn": ["Uses pytest"]}
        assert validate_review(review) == []

    def test_replan_with_reason(self):
        review = {"status": "replan", "reason": "File not found", "learn": None}
        assert validate_review(review) == []

    def test_replan_with_reason_and_learn(self):
        review = {"status": "replan", "reason": "Wrong path", "learn": ["Project is in /opt"]}
        assert validate_review(review) == []

    @pytest.mark.parametrize("status", ["replan", "stuck"])
    def test_non_ok_without_reason_invalid(self, status):
        review = {"status": status, "reason": None, "learn": None}
        errors = validate_review(review)
        assert any("non-null, non-empty reason" in e for e in errors)

    @pytest.mark.parametrize("status", ["replan", "stuck"])
    def test_non_ok_empty_reason_invalid(self, status):
        review = {"status": status, "reason": "", "learn": None}
        errors = validate_review(review)
        assert any("non-null, non-empty reason" in e for e in errors)

    def test_stuck_with_reason(self):
        review = {"status": "stuck", "reason": "CAPTCHA requires human verification", "learn": None}
        assert validate_review(review) == []

    def test_stuck_in_statuses(self):
        assert REVIEW_STATUS_STUCK in REVIEW_STATUSES

    def test_stuck_in_schema_enum(self):
        enum_values = REVIEW_SCHEMA["json_schema"]["schema"]["properties"]["status"]["enum"]
        assert "stuck" in enum_values

    def test_invalid_status(self):
        review = {"status": "maybe", "reason": None, "learn": None}
        errors = validate_review(review)
        assert any("stuck" in e for e in errors)

    def test_missing_status(self):
        review = {"reason": None, "learn": None}
        errors = validate_review(review)
        assert len(errors) >= 1


# --- M83: JSON-Schema validation for PLAN_SCHEMA and REVIEW_SCHEMA ---


_PLAN_SCHEMA_INNER = PLAN_SCHEMA["json_schema"]["schema"]
_REVIEW_SCHEMA_INNER = REVIEW_SCHEMA["json_schema"]["schema"]
_MSG_TASK_DICT = {"type": "msg", "detail": "Hello", "skill": None, "args": None, "expect": None}


class TestM83PlanSchema:
    """M83: PLAN_SCHEMA inner schema accepts valid plans and rejects invalid ones."""

    def _valid(self, instance):
        _jsonschema.validate(instance=instance, schema=_PLAN_SCHEMA_INNER)

    def _invalid(self, instance):
        with pytest.raises(_jsonschema.ValidationError):
            _jsonschema.validate(instance=instance, schema=_PLAN_SCHEMA_INNER)

    def _plan(self, **overrides):
        base = {"goal": "Do X", "secrets": None, "tasks": [{**_MSG_TASK_DICT}], "extend_replan": None}
        base.update(overrides)
        return base

    # Valid ---

    def test_valid_minimal(self):
        self._valid(self._plan())

    def test_valid_secrets_array(self):
        self._valid(self._plan(secrets=[{"key": "K", "value": "V"}]))

    def test_valid_extend_replan_integer(self):
        self._valid(self._plan(extend_replan=3))

    @pytest.mark.parametrize("t", ["exec", "msg", "skill", "search", "replan"])
    def test_valid_task_type(self, t):
        self._valid(self._plan(tasks=[{"type": t, "detail": "x", "skill": None, "args": None, "expect": None}]))

    # Invalid ---

    def test_missing_goal(self):
        d = self._plan()
        del d["goal"]
        self._invalid(d)

    def test_missing_extend_replan(self):
        d = self._plan()
        del d["extend_replan"]
        self._invalid(d)

    def test_extra_top_level_field(self):
        self._invalid(self._plan(unexpected="boom"))

    def test_task_invalid_type_enum(self):
        self._invalid(self._plan(tasks=[{"type": "fly", "detail": "x", "skill": None, "args": None, "expect": None}]))

    def test_task_missing_required_field(self):
        # missing "expect"
        self._invalid(self._plan(tasks=[{"type": "msg", "detail": "x", "skill": None, "args": None}]))

    def test_task_extra_field(self):
        self._invalid(self._plan(tasks=[{**_MSG_TASK_DICT, "extra": "x"}]))

    def test_extend_replan_wrong_type(self):
        self._invalid(self._plan(extend_replan="three"))


class TestM83ReviewSchema:
    """M83: REVIEW_SCHEMA inner schema accepts valid reviews and rejects invalid ones."""

    def _valid(self, instance):
        _jsonschema.validate(instance=instance, schema=_REVIEW_SCHEMA_INNER)

    def _invalid(self, instance):
        with pytest.raises(_jsonschema.ValidationError):
            _jsonschema.validate(instance=instance, schema=_REVIEW_SCHEMA_INNER)

    # Valid ---

    def test_valid_ok_nulls(self):
        self._valid({"status": "ok", "reason": None, "learn": None, "retry_hint": None, "summary": None})

    def test_valid_replan_full(self):
        self._valid({"status": "replan", "reason": "wrong path", "learn": ["Fact 1"], "retry_hint": "try /opt", "summary": "Found page with title X"})

    def test_valid_learn_exactly_3(self):
        self._valid({"status": "ok", "reason": None, "learn": ["a", "b", "c"], "retry_hint": None, "summary": None})

    # Invalid ---

    def test_missing_status(self):
        self._invalid({"reason": None, "learn": None, "retry_hint": None, "summary": None})

    def test_status_not_in_enum(self):
        self._invalid({"status": "maybe", "reason": None, "learn": None, "retry_hint": None, "summary": None})

    def test_missing_retry_hint(self):
        self._invalid({"status": "ok", "reason": None, "learn": None, "summary": None})

    def test_extra_field(self):
        self._invalid({"status": "ok", "reason": None, "learn": None, "retry_hint": None, "summary": None, "extra": "x"})

    def test_learn_exceeds_max_items(self):
        self._invalid({"status": "ok", "reason": None, "learn": ["a", "b", "c", "d"], "retry_hint": None, "summary": None})

    def test_learn_non_string_item(self):
        self._invalid({"status": "ok", "reason": None, "learn": [42], "retry_hint": None, "summary": None})


# --- build_reviewer_messages ---

class TestBuildReviewerMessages:
    def test_is_sync_function(self):
        """M66e: build_reviewer_messages must be a plain def, not async def."""
        import inspect
        assert not inspect.iscoroutinefunction(build_reviewer_messages), (
            "build_reviewer_messages has no awaits and must be a regular function"
        )

    def test_returns_list_directly(self):
        """Calling without await returns a list immediately (not a coroutine)."""
        import inspect
        result = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m"
        )
        assert not inspect.iscoroutine(result), "Must not return a coroutine"
        assert isinstance(result, list)

    async def test_basic_structure(self):
        msgs = build_reviewer_messages(
            goal="Test goal",
            detail="echo hello",
            expect="prints hello",
            output="hello\n",
            user_message="run echo hello",
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    async def test_contains_all_context(self):
        msgs = build_reviewer_messages(
            goal="List files",
            detail="ls -la",
            expect="shows files",
            output="file1.txt\nfile2.txt",
            user_message="list directory contents",
        )
        content = msgs[1]["content"]
        assert "## Plan Context" in content
        assert "List files" in content
        assert "## Task Detail" in content
        assert "ls -la" in content
        assert "## Expected Outcome" in content
        assert "shows files" in content
        assert "## Actual Output" in content
        assert "file1.txt" in content
        assert "## Original User Message" in content
        assert "list directory contents" in content

    async def test_output_fenced(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="some output",
            user_message="msg",
        )
        content = msgs[1]["content"]
        assert "<<<TASK_OUTPUT_" in content
        assert "some output" in content

    async def test_uses_reviewer_system_prompt(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
        )
        assert "task reviewer" in msgs[0]["content"]

    async def test_custom_reviewer_prompt(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "reviewer.md").write_text("My custom reviewer")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            msgs = build_reviewer_messages(
                goal="g", detail="d", expect="e", output="o", user_message="m",
            )
        assert msgs[0]["content"] == "My custom reviewer"

    # --- 21e: success param in reviewer context ---

    async def test_success_true_shows_succeeded(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=True,
        )
        content = msgs[1]["content"]
        assert "## Command Status" in content
        assert "succeeded (exit code 0)" in content

    async def test_success_false_shows_failed(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False,
        )
        content = msgs[1]["content"]
        assert "## Command Status" in content
        assert "FAILED (non-zero exit code)" in content

    async def test_success_none_no_command_status(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=None,
        )
        content = msgs[1]["content"]
        assert "## Command Status" not in content

    async def test_success_default_no_command_status(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
        )
        content = msgs[1]["content"]
        assert "## Command Status" not in content

    # --- M106: exit_code parameter ---

    async def test_exit_code_0_shows_success(self):
        """exit_code=0 with success=True shows 'Exit code: 0 (success)'."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=True, exit_code=0,
        )
        content = msgs[1]["content"]
        assert "Exit code: 0 (success)" in content

    async def test_exit_code_1_shows_note(self):
        """exit_code=1 includes note about grep/which/find."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=1,
        )
        content = msgs[1]["content"]
        assert "Exit code: 1 (non-zero)" in content
        assert "no matches found" in content

    async def test_exit_code_127_shows_note(self):
        """exit_code=127 includes note about command not found."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=127,
        )
        content = msgs[1]["content"]
        assert "Exit code: 127 (non-zero)" in content
        assert "not found in PATH" in content

    async def test_exit_code_126_shows_note(self):
        """exit_code=126 includes note about permission issue."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=126,
        )
        content = msgs[1]["content"]
        assert "Exit code: 126 (non-zero)" in content
        assert "not executable" in content

    async def test_exit_code_neg1_shows_timeout_note(self):
        """exit_code=-1 includes note about timeout/OS error."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=-1,
        )
        content = msgs[1]["content"]
        assert "Exit code: -1 (non-zero)" in content
        assert "killed" in content.lower()

    async def test_exit_code_2_shows_syntax_note(self):
        """exit_code=2 includes note about usage/syntax error."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=2,
        )
        content = msgs[1]["content"]
        assert "Exit code: 2 (non-zero)" in content
        assert "usage" in content.lower() or "syntax" in content.lower()

    async def test_exit_code_unknown_no_note(self):
        """Unknown exit code (e.g. 42) shows code but no note."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=42,
        )
        content = msgs[1]["content"]
        assert "Exit code: 42 (non-zero)" in content
        assert "Note:" not in content

    async def test_exit_code_none_backward_compat(self):
        """exit_code=None (default) falls back to old format."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            success=False, exit_code=None,
        )
        content = msgs[1]["content"]
        assert "FAILED (non-zero exit code)" in content

    async def test_safety_rules_injected(self):
        """M412: safety rules appear in reviewer context."""
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            safety_rules=["Never delete /data", "Production DB is read-only"],
        )
        content = msgs[1]["content"]
        assert "## Safety Rules" in content
        assert "Never delete /data" in content
        assert "Production DB is read-only" in content

    async def test_no_safety_section_when_empty(self):
        """M412: no safety section when safety_rules is None/empty."""
        msgs1 = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            safety_rules=None,
        )
        assert "Safety Rules" not in msgs1[1]["content"]
        msgs2 = build_reviewer_messages(
            goal="g", detail="d", expect="e", output="o", user_message="m",
            safety_rules=[],
        )
        assert "Safety Rules" not in msgs2[1]["content"]


# --- run_reviewer ---

VALID_REVIEW_OK = json.dumps({"status": "ok", "reason": None, "learn": None, "retry_hint": None})
VALID_REVIEW_REPLAN = json.dumps({"status": "replan", "reason": "File missing", "learn": None, "retry_hint": None})
VALID_REVIEW_WITH_LEARN = json.dumps({"status": "ok", "reason": None, "learn": ["Uses Python 3.12"], "retry_hint": None})
INVALID_REVIEW = json.dumps({"status": "replan", "reason": None, "learn": None, "retry_hint": None})


class TestRunReviewer:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(reviewer="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )

    async def test_ok_first_try(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_OK):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["status"] == "ok"
        assert review["reason"] is None
        assert review["learn"] is None

    async def test_replan_first_try(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_REPLAN):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["status"] == "replan"
        assert review["reason"] == "File missing"

    async def test_exit_code_forwarded_to_messages(self, config):
        """run_reviewer forwards exit_code to build_reviewer_messages."""
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return VALID_REVIEW_OK

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_reviewer(
                config, "goal", "detail", "expect", "output", "msg",
                success=False, exit_code=127,
            )
        user_content = captured_messages[1]["content"]
        assert "Exit code: 127" in user_content
        assert "not found in PATH" in user_content

    async def test_review_with_learning(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_REVIEW_WITH_LEARN):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["learn"] == ["Uses Python 3.12"]

    async def test_retry_on_invalid_then_valid(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=[INVALID_REVIEW, VALID_REVIEW_REPLAN]):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["status"] == "replan"
        assert review["reason"] == "File missing"

    async def test_all_retries_exhausted(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=INVALID_REVIEW):
            with pytest.raises(ReviewError, match="validation failed after 3"):
                await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

    async def test_llm_error_raises_review_error(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(ReviewError, match="LLM call failed.*API down"):
                await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

    async def test_invalid_json_raises_review_error(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            with pytest.raises(ReviewError, match="(?i)invalid JSON"):
                await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

    async def test_retry_appends_error_feedback(self, config):
        """On retry, error feedback and previous assistant response are appended."""
        call_messages = []

        async def _capture_call(cfg, role, messages, **kw):
            call_messages.append(len(messages))
            if len(call_messages) == 1:
                return INVALID_REVIEW
            return VALID_REVIEW_REPLAN

        with patch("kiso.brain.call_llm", side_effect=_capture_call):
            await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

        # First call: system + user = 2 messages
        assert call_messages[0] == 2
        # Second call: +assistant (bad review) +user (error feedback) = 4
        assert call_messages[1] == 4

    async def test_passes_review_schema(self, config):
        """run_reviewer passes REVIEW_SCHEMA as response_format."""
        captured_kwargs = {}

        async def _capture(cfg, role, messages, **kw):
            captured_kwargs.update(kw)
            return VALID_REVIEW_OK

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_reviewer(config, "goal", "detail", "expect", "output", "msg")

        assert captured_kwargs["response_format"] == REVIEW_SCHEMA


# --- M9: validate_curator ---

class TestValidateCurator:
    def test_promote_valid(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "question": None, "reason": "Good fact",
             "entity_name": "myproject", "entity_kind": "project"},
        ]}
        assert validate_curator(result) == []

    def test_ask_valid(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "ask", "fact": None, "question": "Which DB?", "reason": "Need clarity"},
        ]}
        assert validate_curator(result) == []

    def test_discard_valid(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "discard", "fact": None, "question": None, "reason": "Transient"},
        ]}
        assert validate_curator(result) == []

    def test_promote_missing_fact(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": None, "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result)
        assert any("promote verdict requires a non-empty fact" in e for e in errors)

    def test_ask_missing_question(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "ask", "fact": None, "question": None, "reason": "Need info"},
        ]}
        errors = validate_curator(result)
        assert any("ask verdict requires a non-empty question" in e for e in errors)

    def test_missing_reason(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "discard", "fact": None, "question": None, "reason": ""},
        ]}
        errors = validate_curator(result)
        assert any("reason is required" in e for e in errors)

    def test_multiple_evaluations(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Project uses Python 3.11", "question": None, "reason": "Good",
             "entity_name": "myproject", "entity_kind": "project"},
            {"learning_id": 2, "verdict": "discard", "fact": None, "question": None, "reason": "Noise"},
            {"learning_id": 3, "verdict": "ask", "fact": None, "question": "What DB?", "reason": "Unclear"},
        ]}
        assert validate_curator(result) == []

    def test_validate_curator_fewer_than_expected_ok(self):
        """M322: fewer evaluations than learnings is OK (consolidation)."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Consolidated fact here", "question": None, "reason": "Good",
             "entity_name": "myproject", "entity_kind": "project"},
        ]}
        assert validate_curator(result, expected_count=3) == []

    def test_validate_curator_more_than_expected_error(self):
        """M322: more evaluations than learnings is an error."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Fact A is valid", "question": None, "reason": "Good",
             "entity_name": "myproject", "entity_kind": "project"},
            {"learning_id": 2, "verdict": "discard", "fact": None, "question": None, "reason": "Noise"},
            {"learning_id": 3, "verdict": "discard", "fact": None, "question": None, "reason": "Noise"},
        ]}
        errors = validate_curator(result, expected_count=2)
        assert any("at most 2" in e for e in errors)

    def test_validate_curator_no_count_check(self):
        """No error when expected_count is None (backwards compat)."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Some valid fact here", "question": None, "reason": "Good",
             "entity_name": "myproject", "entity_kind": "project"},
        ]}
        assert validate_curator(result, expected_count=None) == []

    def test_validate_curator_short_fact_error(self):
        """M322: promoted fact with < 10 chars fails validation."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Short", "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result)
        assert any("too short" in e for e in errors)

    def test_validate_curator_fact_exactly_10_ok(self):
        """M322: promoted fact with exactly 10 chars passes."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "1234567890", "question": None, "reason": "Good",
             "entity_name": "myproject", "entity_kind": "project"},
        ]}
        assert validate_curator(result) == []


# --- M9: build_curator_messages ---

class TestBuildCuratorMessages:
    def test_formats_learnings(self):
        learnings = [
            {"id": 1, "content": "Uses Flask"},
            {"id": 2, "content": "Database is PostgreSQL"},
        ]
        msgs = build_curator_messages(learnings)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "[id=1] Uses Flask" in msgs[1]["content"]
        assert "[id=2] Database is PostgreSQL" in msgs[1]["content"]

    def test_uses_curator_system_prompt(self):
        msgs = build_curator_messages([{"id": 1, "content": "test"}])
        assert "knowledge curator" in msgs[0]["content"]

    def test_available_tags_injected(self):
        """M249: available tags are included in the curator prompt."""
        msgs = build_curator_messages(
            [{"id": 1, "content": "test"}],
            available_tags=["browser", "tech-stack", "api"],
        )
        user_content = msgs[1]["content"]
        assert "## Existing Tags" in user_content
        assert "browser" in user_content
        assert "tech-stack" in user_content

    def test_no_tags_section_without_available_tags(self):
        """No Existing Tags section when available_tags is empty or None."""
        msgs = build_curator_messages([{"id": 1, "content": "test"}])
        assert "## Existing Tags" not in msgs[1]["content"]
        msgs2 = build_curator_messages([{"id": 1, "content": "test"}], available_tags=[])
        assert "## Existing Tags" not in msgs2[1]["content"]


# --- M9: run_curator ---

VALID_CURATOR = json.dumps({"evaluations": [
    {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": "project", "question": None, "reason": "Good", "tags": ["tech-stack"], "entity_name": "myproject", "entity_kind": "project"},
]})

INVALID_CURATOR = json.dumps({"evaluations": [
    {"learning_id": 1, "verdict": "promote", "fact": None, "category": None, "question": None, "reason": "Good", "tags": None},
]})


class TestRunCurator:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(curator="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )

    async def test_success(self, config):
        learnings = [{"id": 1, "content": "Uses Python"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_CURATOR):
            result = await run_curator(config, learnings)
        assert len(result["evaluations"]) == 1
        assert result["evaluations"][0]["verdict"] == "promote"

    async def test_entities_forwarded_to_messages(self, config):
        """M344: run_curator forwards available_entities to build_curator_messages."""
        learnings = [{"id": 1, "content": "Uses Python"}]
        entities = [{"name": "flask", "kind": "tool"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_CURATOR) as mock_llm:
            await run_curator(config, learnings, available_entities=entities)
        # The user message should contain the entities section
        messages = mock_llm.call_args[1].get("messages") or mock_llm.call_args[0][2]
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert "flask" in user_msg["content"]
        assert "## Existing Entities" in user_msg["content"]

    async def test_validation_retry(self, config):
        learnings = [{"id": 1, "content": "Uses Python"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=[INVALID_CURATOR, VALID_CURATOR]):
            result = await run_curator(config, learnings)
        assert result["evaluations"][0]["fact"] == "Uses Python"

    async def test_llm_error_raises_curator_error(self, config):
        learnings = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(CuratorError, match="LLM call failed"):
                await run_curator(config, learnings)

    async def test_all_retries_exhausted(self, config):
        learnings = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=INVALID_CURATOR):
            with pytest.raises(CuratorError, match="validation failed after 3"):
                await run_curator(config, learnings)

    async def test_invalid_json_raises_curator_error(self, config):
        learnings = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            with pytest.raises(CuratorError, match="(?i)invalid JSON"):
                await run_curator(config, learnings)


# --- M9: build_summarizer_messages ---

class TestBuildSummarizerMessages:
    def test_includes_summary_and_messages(self):
        messages = [
            {"role": "user", "user": "alice", "content": "Hello"},
            {"role": "system", "content": "Hi there"},
        ]
        msgs = build_summarizer_messages("Previous summary", messages)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert "## Current Summary" in msgs[1]["content"]
        assert "Previous summary" in msgs[1]["content"]
        assert "## Messages" in msgs[1]["content"]
        assert "Hello" in msgs[1]["content"]

    def test_no_summary_omits_section(self):
        messages = [{"role": "user", "user": "alice", "content": "Hello"}]
        msgs = build_summarizer_messages("", messages)
        assert "## Current Summary" not in msgs[1]["content"]
        assert "## Messages" in msgs[1]["content"]


# --- M9: run_summarizer ---

class TestRunSummarizer:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(summarizer="gpt-4"),
            settings=_full_settings(),
            raw={},
        )

    async def test_success(self, config):
        messages = [{"role": "user", "user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="Updated summary"):
            result = await run_summarizer(config, "Old summary", messages)
        assert result == "Updated summary"

    async def test_llm_error_raises_summarizer_error(self, config):
        messages = [{"role": "user", "user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(SummarizerError, match="LLM call failed"):
                await run_summarizer(config, "", messages)


# --- M9: run_fact_consolidation ---

class TestRunFactConsolidation:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(summarizer="gpt-4"),
            settings=_full_settings(),
            raw={},
        )

    async def test_returns_list_of_dicts(self, config):
        facts = [
            {"id": 1, "content": "Uses Python"},
            {"id": 2, "content": "Uses Python 3.12"},
        ]
        llm_response = json.dumps([
            {"content": "Uses Python 3.12", "category": "project", "confidence": 1.0}
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "Uses Python 3.12"
        assert result[0]["category"] == "project"
        assert result[0]["confidence"] == 1.0

    async def test_backward_compat_plain_strings(self, config):
        """LLM returns plain strings → wrapped into dicts with defaults."""
        facts = [
            {"id": 1, "content": "Uses Python"},
            {"id": 2, "content": "Uses Python 3.12"},
        ]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value='["Uses Python 3.12"]'):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "Uses Python 3.12"
        assert result[0]["category"] == "general"
        assert result[0]["confidence"] == 1.0

    async def test_dict_with_defaults(self, config):
        """Dict with only content key → category and confidence get defaults."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([{"content": "test fact"}])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["category"] == "general"
        assert result[0]["confidence"] == 1.0

    async def test_invalid_items_skipped(self, config):
        """Items that are not dicts or strings are skipped."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "valid", "category": "project", "confidence": 0.9},
            42,
            None,
            {"no_content_key": True},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "valid"

    async def test_llm_error_raises_summarizer_error(self, config):
        facts = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("down")):
            with pytest.raises(SummarizerError, match="LLM call failed"):
                await run_fact_consolidation(config, facts)

    async def test_invalid_json_raises_error(self, config):
        facts = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            with pytest.raises(SummarizerError, match="invalid JSON"):
                await run_fact_consolidation(config, facts)

    async def test_non_array_raises_error(self, config):
        facts = [{"id": 1, "content": "test"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value='{"not": "array"}'):
            with pytest.raises(SummarizerError, match="must return a JSON array"):
                await run_fact_consolidation(config, facts)

    async def test_confidence_non_numeric_string_falls_back(self, config):
        """M84d: non-numeric confidence string must fall back to 1.0 without crashing."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "Uses Python", "confidence": "high"},
            {"content": "Uses Linux", "confidence": None},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 2
        assert result[0]["confidence"] == 1.0
        assert result[1]["confidence"] == 1.0

    async def test_confidence_clamped_to_unit_interval(self, config):
        """M37: confidence values outside [0.0, 1.0] are clamped."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "high", "confidence": 99.9},
            {"content": "low", "confidence": -5.0},
            {"content": "normal", "confidence": 0.7},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["confidence"] == 1.0   # clamped from 99.9
        assert result[1]["confidence"] == 0.0   # clamped from -5.0
        assert result[2]["confidence"] == 0.7   # unchanged

    async def test_item_count_cap_truncates_oversized_result(self, config):
        """M87e: LLM returning more than _MAX_CONSOLIDATION_ITEMS items is truncated."""
        from kiso.brain import _MAX_CONSOLIDATION_ITEMS
        facts = [{"id": 1, "content": "test"}]
        oversized = [{"content": f"fact {i}", "category": "general"} for i in range(_MAX_CONSOLIDATION_ITEMS + 50)]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=json.dumps(oversized)):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == _MAX_CONSOLIDATION_ITEMS

    async def test_unknown_category_normalized_to_general(self, config):
        """M87e: unrecognized category strings are normalized to 'general'."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "fact with bad category", "category": "injected"},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["category"] == "general"

    async def test_null_category_normalized_to_general(self, config):
        """M87e: null category is normalized to 'general'."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "fact with null category", "category": None},
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert result[0]["category"] == "general"

    async def test_empty_and_whitespace_content_filtered(self, config):
        """M87e: items with empty or whitespace-only content are filtered out."""
        facts = [{"id": 1, "content": "test"}]
        llm_response = json.dumps([
            {"content": "valid fact"},
            {"content": ""},
            {"content": "  "},
            {"content": "ab"},
            " ",
            "",
        ])
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=llm_response):
            result = await run_fact_consolidation(config, facts)
        assert len(result) == 1
        assert result[0]["content"] == "valid fact"

    async def test_valid_categories_preserved(self, config):
        """M87e: all four valid categories are accepted as-is."""
        from kiso.brain import _VALID_FACT_CATEGORIES
        facts = [{"id": 1, "content": "test"}]
        items = [{"content": f"fact for {cat}", "category": cat} for cat in sorted(_VALID_FACT_CATEGORIES)]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=json.dumps(items)):
            result = await run_fact_consolidation(config, facts)
        result_cats = {r["category"] for r in result}
        assert result_cats == _VALID_FACT_CATEGORIES


# --- M9: _load_system_prompt for curator/summarizer ---

class TestLoadSystemPromptCuratorSummarizer:
    def test_curator_default(self):
        prompt = _load_system_prompt("curator")
        assert "knowledge curator" in prompt

    def test_summarizer_session_default(self):
        prompt = _load_system_prompt("summarizer-session")
        assert "session summarizer" in prompt
        assert "Key Decisions" in prompt
        assert "Open Questions" in prompt
        assert "Working Knowledge" in prompt

    def test_summarizer_facts_default(self):
        prompt = _load_system_prompt("summarizer-facts")
        assert "fact" in prompt.lower()
        assert "category" in prompt.lower()
        assert "confidence" in prompt.lower()

    def test_paraphraser_default(self):
        prompt = _load_system_prompt("paraphraser")
        assert "paraphraser" in prompt

    def test_m285_session_summarizer_english_output(self):
        """M285: session summarizer writes in English, preserves domain terms."""
        prompt = (_ROLES_DIR / "summarizer-session.md").read_text()
        assert "English" in prompt
        assert "original language" in prompt.lower()

    def test_m285_facts_summarizer_english_output(self):
        """M285: facts summarizer writes in English, preserves identifiers."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "English" in prompt
        assert "proper nouns" in prompt


# --- M10: Paraphraser ---

class TestBuildParaphraserMessages:
    def test_formats_messages(self):
        messages = [
            {"user": "alice", "content": "Hello there"},
            {"user": "bob", "content": "How are you?"},
        ]
        msgs = build_paraphraser_messages(messages)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "[alice]: Hello there" in msgs[1]["content"]
        assert "[bob]: How are you?" in msgs[1]["content"]

    def test_missing_user_defaults_unknown(self):
        messages = [{"content": "test message"}]
        msgs = build_paraphraser_messages(messages)
        assert "[unknown]: test message" in msgs[1]["content"]


class TestRunParaphraser:
    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(paraphraser="gpt-4"),
            settings=_full_settings(),
            raw={},
        )

    async def test_run_paraphraser_success(self, config):
        messages = [{"user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="The user greeted the assistant."):
            result = await run_paraphraser(config, messages)
        assert result == "The user greeted the assistant."

    async def test_run_paraphraser_error(self, config):
        messages = [{"user": "alice", "content": "Hello"}]
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(ParaphraserError, match="LLM call failed"):
                await run_paraphraser(config, messages)


# --- M10: Fencing in planner messages ---

class TestPlannerMessagesFencing:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(context_messages=3),
            raw={},
        )

    async def test_planner_messages_fence_recent(self, db, config):
        await create_session(db, "sess1")
        await save_message(db, "sess1", "alice", "user", "hello world")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "new msg")
        content = msgs[1]["content"]
        assert "<<<MESSAGES_" in content
        assert "<<<END_MESSAGES_" in content

    async def test_planner_messages_fence_new_message(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "test input")
        content = msgs[1]["content"]
        assert "<<<USER_MSG_" in content
        assert "<<<END_USER_MSG_" in content
        assert "test input" in content

    async def test_planner_messages_include_paraphrased(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed, *_ = await build_planner_messages(
            db, config, "sess1", "admin", "hello",
            paraphrased_context="The external user asked about the weather.",
        )
        content = msgs[1]["content"]
        assert "## Paraphrased External Messages (untrusted)" in content
        assert "<<<PARAPHRASED_" in content
        assert "The external user asked about the weather." in content

    async def test_planner_messages_no_paraphrased_when_none(self, db, config):
        await create_session(db, "sess1")
        msgs, _installed, *_ = await build_planner_messages(db, config, "sess1", "admin", "hello")
        content = msgs[1]["content"]
        assert "Paraphrased" not in content


# --- M10: Fencing in reviewer messages ---

class TestReviewerMessagesFencing:
    async def test_reviewer_messages_fence_output(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="some task output",
            user_message="user msg",
        )
        content = msgs[1]["content"]
        assert "<<<TASK_OUTPUT_" in content
        assert "<<<END_TASK_OUTPUT_" in content
        assert "some task output" in content

    async def test_reviewer_messages_fence_user_message(self):
        msgs = build_reviewer_messages(
            goal="g", detail="d", expect="e",
            output="output",
            user_message="the original user message",
        )
        content = msgs[1]["content"]
        assert "<<<USER_MSG_" in content
        assert "<<<END_USER_MSG_" in content
        assert "the original user message" in content


# --- _strip_fences ---


class TestStripFences:
    def test_no_fences(self):
        assert _strip_fences('{"key": "value"}') == '{"key": "value"}'

    def test_json_fence(self):
        assert _strip_fences('```json\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_plain_fence(self):
        assert _strip_fences('```\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_leading_whitespace(self):
        assert _strip_fences(' ```json\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_trailing_whitespace(self):
        assert _strip_fences('```json\n{"key": "value"}\n``` ') == '{"key": "value"}'

    def test_bare_json(self):
        raw = '{"goal": "test", "secrets": null, "tasks": []}'
        assert _strip_fences(raw) == raw

    def test_empty_string(self):
        assert _strip_fences('') == ''


# --- Messenger ---

def _make_brain_config(**overrides) -> Config:
    base_settings = _full_settings()
    if "settings" in overrides:
        base_settings.update(overrides.pop("settings"))
    base_models = _full_models(messenger="gpt-4")
    if "models" in overrides:
        base_models.update(overrides.pop("models"))
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"local": Provider(base_url="http://localhost:11434/v1")},
        users={},
        models=base_models,
        settings=base_settings,
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestDefaultMessengerPrompt:
    def test_contains_placeholder(self):
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "{bot_name}" in prompt

    def test_verbatim_instructions_rule(self):
        """M46: messenger prompt must instruct to reproduce setup instructions verbatim."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "verbatim" in prompt

    def test_load_replaces_bot_name(self):
        config = _make_brain_config(settings={"bot_name": "TestBot"})
        msgs = build_messenger_messages(config, "", [], "say hi")
        system_prompt = msgs[0]["content"]
        assert "TestBot" in system_prompt
        assert "{bot_name}" not in system_prompt

    def test_m5_no_fabricate_absent_info(self):
        """M5.3: messenger must not fabricate entries for absent information."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "fabricate" in prompt.lower()
        assert "nothing is needed" in prompt or "nothing was found" in prompt

    def test_m138_no_invent_commands(self):
        """M138: messenger must never invent CLI commands or code snippets."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Never invent CLI commands" in prompt
        assert "verbatim in preceding task outputs" in prompt or "verbatim in the preceding task outputs" in prompt

    def test_m214_language_inference_from_user_message(self):
        """M214: messenger prompt tells LLM to infer language from user message."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Original User Message" in prompt

    def test_m264_system_actions_identity(self):
        """M264: messenger describes system actions, never says 'I cannot'."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "system actions" in prompt
        assert 'Never say "I cannot"' in prompt

    def test_m277_voice_rules(self):
        """M277/M424: messenger has explicit voice rules for system vs conversational."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Voice rules" in prompt
        assert '"I ran"' in prompt
        assert "third-person" in prompt or "passive" in prompt

    def test_m424_language_consolidated(self):
        """M424: messenger has single consolidated language block."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Language:" in prompt
        assert "one language" in prompt
        assert "Never echo the language instruction" in prompt

    def test_m351_language_fallback_english_only_when_all_english(self):
        """M351/M424: English fallback only when all user messages are English."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "English only when all user messages are English" in prompt

    def test_m351_recent_messages_language_inference(self):
        """M351: messenger infers language from Recent Messages when no instruction."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Recent Messages" in prompt
        assert "most recent user message" in prompt


class TestBuildMessengerMessages:
    def test_basic_structure(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "## Task\nsay hi" in msgs[1]["content"]

    def test_includes_summary(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "User is working on Flask app", [], "say hi")
        assert "Flask app" in msgs[1]["content"]

    def test_includes_facts(self):
        config = _make_brain_config()
        facts = [{"content": "Uses Python 3.12"}]
        msgs = build_messenger_messages(config, "", facts, "say hi")
        assert "Python 3.12" in msgs[1]["content"]

    def test_includes_plan_outputs(self):
        config = _make_brain_config()
        outputs_text = "[1] exec: echo hi\nStatus: done\nhi"
        msgs = build_messenger_messages(config, "", [], "report", outputs_text)
        assert "## Preceding Task Outputs" in msgs[1]["content"]
        assert "echo hi" in msgs[1]["content"]

    def test_no_outputs_section_when_empty(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi", "")
        assert "Preceding Task Outputs" not in msgs[1]["content"]

    def test_includes_goal(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi", goal="List files")
        assert "## Current User Request\nList files" in msgs[1]["content"]

    def test_no_goal_section_when_empty(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi")
        assert "Current User Request" not in msgs[1]["content"]

    def test_goal_appears_before_summary(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "old context", [], "say hi", goal="new question",
        )
        content = msgs[1]["content"]
        goal_pos = content.index("Current User Request")
        summary_pos = content.index("Session Summary")
        assert goal_pos < summary_pos


    def test_includes_user_message(self):
        """M214: user_message adds Original User Message section."""
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "", [], "say hi", user_message="Ciao, come stai?",
        )
        content = msgs[1]["content"]
        assert "## Original User Message" in content
        assert "Ciao, come stai?" in content

    def test_no_user_message_section_when_empty(self):
        """M214: no section when user_message is empty."""
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi", user_message="")
        assert "Original User Message" not in msgs[1]["content"]

    def test_user_message_appears_before_goal(self):
        """M214: user message section comes before goal."""
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "", [], "say hi", goal="Do stuff", user_message="fammi qualcosa",
        )
        content = msgs[1]["content"]
        user_pos = content.index("Original User Message")
        goal_pos = content.index("Current User Request")
        assert user_pos < goal_pos


    def test_briefing_context_replaces_summary_facts(self):
        """M260: briefing_context replaces raw summary and facts."""
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "Old summary", [{"content": "Old fact"}], "say hi",
            briefing_context="Synthesized context from briefer.",
        )
        content = msgs[1]["content"]
        assert "## Context\nSynthesized context from briefer." in content
        assert "## Session Summary" not in content
        assert "## Known Facts" not in content

    def test_no_briefing_context_uses_raw(self):
        """M260: without briefing_context, raw summary/facts are used."""
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "Session summary here", [{"content": "A fact"}], "say hi",
        )
        content = msgs[1]["content"]
        assert "## Session Summary" in content
        assert "## Known Facts" in content
        assert "## Context\n" not in content


class TestRunMessenger:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_successful_call(self, db):
        config = _make_brain_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="Ciao!"):
            result = await run_messenger(db, config, "sess1", "Greet the user")
        assert result == "Ciao!"

    async def test_uses_messenger_role(self, db):
        config = _make_brain_config()
        captured = {}

        async def _capture(cfg, role, messages, **kw):
            captured["role"] = role
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(db, config, "sess1", "say hi")
        assert captured["role"] == "messenger"

    async def test_goal_passed_to_context(self, db):
        """run_messenger passes goal to build_messenger_messages context."""
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(db, config, "sess1", "say hi", goal="List files")
        user_content = captured_messages[1]["content"]
        assert "Current User Request" in user_content
        assert "List files" in user_content

    async def test_user_message_passed_to_context(self, db):
        """M214: run_messenger forwards user_message to context."""
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(
                db, config, "sess1", "say hi",
                user_message="Dimmi come va",
            )
        user_content = captured_messages[1]["content"]
        assert "Original User Message" in user_content
        assert "Dimmi come va" in user_content

    async def test_llm_error_raises_messenger_error(self, db):
        config = _make_brain_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(MessengerError, match="API down"):
                await run_messenger(db, config, "sess1", "say hi")

    async def test_loads_custom_role_file(self, db, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "messenger.md").write_text("You are {bot_name}, a pirate assistant.")
        config = _make_brain_config(settings={"bot_name": "Arrr"})
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture), \
             patch("kiso.brain.KISO_DIR", tmp_path):
            await run_messenger(db, config, "sess1", "say hi")

        assert "Arrr" in captured_messages[0]["content"]
        assert "pirate" in captured_messages[0]["content"]

    async def test_custom_role_without_placeholder(self, db, tmp_path):
        """Custom messenger.md without {bot_name} should work fine."""
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "messenger.md").write_text("You are a helpful robot.")
        config = _make_brain_config(settings=_full_settings(bot_name="Kiso"))
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture), \
             patch("kiso.brain.KISO_DIR", tmp_path):
            await run_messenger(db, config, "sess1", "say hi")

        assert "helpful robot" in captured_messages[0]["content"]
        assert "{bot_name}" not in captured_messages[0]["content"]


    async def test_briefing_context_skips_db_queries(self, db):
        """M260: when briefing_context is provided, skip summary/facts DB queries."""
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture), \
             patch("kiso.brain.get_session") as mock_sess, \
             patch("kiso.brain.get_facts") as mock_facts:
            await run_messenger(
                db, config, "sess1", "say hi",
                briefing_context="Briefer context here.",
            )

        # DB queries for summary/facts should NOT be called
        mock_sess.assert_not_called()
        mock_facts.assert_not_called()
        # Briefing context should appear in messenger input
        user_content = captured_messages[1]["content"]
        assert "## Context\nBriefer context here." in user_content
        assert "## Session Summary" not in user_content


class TestLoadSystemPromptMessenger:
    def test_default_messenger_prompt(self):
        prompt = _load_system_prompt("messenger")
        assert "{bot_name}" in prompt
        assert "friendly" in prompt


class TestM369MessengerSanitizer:
    """M369: messenger output sanitization."""

    def test_strips_tool_call_blocks(self):
        text = 'Hello <tool_call>{"name": "search", "arguments": {"q": "test"}}</tool_call> world'
        assert _sanitize_messenger_output(text) == "Hello  world"

    def test_strips_function_call_blocks(self):
        text = 'Hi <function_call>something</function_call> there'
        assert _sanitize_messenger_output(text) == "Hi  there"

    def test_strips_orphaned_tags(self):
        text = 'Text </tool_call> more'
        assert _sanitize_messenger_output(text) == "Text  more"

    def test_preserves_normal_text(self):
        text = "La tua chiave SSH pubblica è: ssh-ed25519 AAAA..."
        assert _sanitize_messenger_output(text) == text

    def test_strips_multiline_tool_call(self):
        text = 'Before\n<tool_call>\n{"name": "x"}\n</tool_call>\nAfter'
        result = _sanitize_messenger_output(text)
        assert "<tool_call>" not in result
        assert "After" in result

    def test_empty_string(self):
        assert _sanitize_messenger_output("") == ""

    async def test_run_messenger_applies_sanitizer(self, tmp_path):
        """run_messenger applies sanitization to LLM output."""
        db = await init_db(tmp_path / "test.db")
        await create_session(db, "sess1")
        config = _make_brain_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value='Ciao! <tool_call>{"name": "x"}</tool_call>'):
            result = await run_messenger(db, config, "sess1", "greet")
        assert result == "Ciao!"
        assert "<tool_call>" not in result
        await db.close()

    def test_messenger_prompt_prohibits_xml(self):
        """M369: messenger prompt forbids XML/tool_call output."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Never emit XML" in prompt
        assert "tool_call" in prompt


# --- Exec Translator ---

class TestBuildExecTranslatorMessages:
    def test_basic_structure(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List all Python files", "OS: Linux\nShell: /bin/sh",
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "## Task\nList all Python files" in msgs[1]["content"]
        assert "## System Environment" in msgs[1]["content"]

    def test_includes_plan_outputs(self):
        config = _make_brain_config()
        outputs_text = "[1] exec: list files\nStatus: done\nfoo.py"
        msgs = build_exec_translator_messages(
            config, "Count them", "OS: Linux", outputs_text,
        )
        assert "## Preceding Task Outputs" in msgs[1]["content"]
        assert "foo.py" in msgs[1]["content"]

    def test_no_outputs_section_when_empty(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List files", "OS: Linux", "",
        )
        assert "Preceding Task Outputs" not in msgs[1]["content"]


class TestRunExecTranslator:
    async def test_successful_translation(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="ls -la *.py"):
            result = await run_exec_translator(
                config, "List all Python files", "OS: Linux",
            )
        assert result == "ls -la *.py"

    async def test_strips_whitespace(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="  ls -la  \n"):
            result = await run_exec_translator(
                config, "List files", "OS: Linux",
            )
        assert result == "ls -la"

    async def test_cannot_translate_raises(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="CANNOT_TRANSLATE"):
            with pytest.raises(ExecTranslatorError, match="Cannot translate"):
                await run_exec_translator(config, "Do something impossible", "OS: Linux")

    async def test_empty_result_raises(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="   "):
            with pytest.raises(ExecTranslatorError, match="Cannot translate"):
                await run_exec_translator(config, "Do something", "OS: Linux")

    async def test_llm_error_raises_translator_error(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("API down")):
            with pytest.raises(ExecTranslatorError, match="API down"):
                await run_exec_translator(config, "List files", "OS: Linux")

    async def test_uses_worker_role(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        captured = {}

        async def _capture(cfg, role, messages, **kw):
            captured["role"] = role
            return "echo hello"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_exec_translator(config, "Say hello", "OS: Linux")
        assert captured["role"] == "worker"


class TestLoadSystemPromptExecTranslator:
    def test_default_prompt(self):
        prompt = _load_system_prompt("worker")
        assert "shell command translator" in prompt
        assert "CANNOT_TRANSLATE" in prompt

    def test_custom_prompt_overrides(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "worker.md").write_text("Custom worker prompt")
        with patch("kiso.brain.KISO_DIR", tmp_path):
            prompt = _load_system_prompt("worker")
        assert prompt == "Custom worker prompt"


# --- Exec translator prompt content ---


class TestExecTranslatorPromptContent:
    def test_exec_translator_prompt_mentions_preceding_outputs(self):
        """The default exec translator prompt should mention Preceding Task Outputs."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "Preceding Task Outputs" in prompt

    def test_m139_bash_not_sh(self):
        """M139: worker prompt references bash, not /bin/sh."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "Executed by bash" in prompt

    def test_m140_large_output_files(self):
        """M140: worker prompt mentions large output files."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "Full output saved to" in prompt

    def test_m141_curl_follow_redirects(self):
        """M141: worker prompt requires curl -L for redirects."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "curl -L" in prompt

    def test_m205_kiso_short_tool_names(self):
        """M205: worker prompt tells translator to use short tool/connector names."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "short tool/connector names" in prompt


# --- Planner prompt content ---


class TestPlannerPromptContent:
    def test_planner_prompt_contains_reference_docs_instruction(self):
        """The default planner prompt should mention confirmed facts and reuse."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "confirmed facts" in prompt.lower()
        assert "never re-investigate" in prompt.lower() or "re-verify" in prompt.lower()

    def test_planner_prompt_contains_replan_task_type(self):
        """The default planner prompt should mention the replan task type."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "replan" in prompt
        assert "investigation" in prompt.lower() or "investigate" in prompt.lower()
        assert "extend_replan" in prompt

    def test_planner_prompt_contains_search_task_type(self):
        """The default planner prompt should mention the search task type."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "search:" in prompt
        assert "web search" in prompt.lower() or "search query" in prompt.lower()

    def test_m40_last_task_rule_marked_critical(self):
        """M40: last-task rule must be prefixed CRITICAL to reduce model skips."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "CRITICAL:" in prompt

    def test_m40_pub_path_distinction_documented(self):
        """M40: pub/ filesystem path vs served URL must be explicitly distinguished."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "filesystem path" in prompt
        # Files served at URLs — must not use the URL as a path
        assert "never use urls as filesystem paths" in prompt.lower()

    def test_m45_plugin_install_uses_registry_not_search(self):
        """M45: planner plugin-install appendix must forbid web search for kiso plugin discovery."""
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "never kiso skill search" in prompt.lower() or "never for plugin discovery" in prompt.lower()
        assert "registry" in prompt.lower()

    def test_m45_plugin_install_rule_is_mandatory(self):
        """M45: plugin installation appendix must be marked MANDATORY."""
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "Plugin installation:" in prompt

    def test_m46_plugin_install_checks_kiso_toml_before_install(self):
        """M46: planner must curl kiso.toml from GitHub before installing to discover env requirements."""
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "raw.githubusercontent.com" in prompt
        assert "kiso.toml" in prompt
        # Step ordering: curl kiso.toml must come before "kiso tool install {name}".
        toml_pos = prompt.index("kiso.toml")
        install_pos = prompt.index("kiso tool install {name}")
        assert toml_pos < install_pos

    def test_m46_plugin_install_includes_env_description_in_msg(self):
        """M46: planner plugin-install appendix must include env var descriptions from kiso.toml."""
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "description" in prompt
        assert "description from kiso.toml" in prompt.lower() or "descriptions from kiso.toml" in prompt.lower() or "include descriptions" in prompt.lower()

    def test_m102a_plugin_discovery_never_single_type_search(self):
        """M102a: planner must never use kiso skill search for initial plugin discovery."""
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "never kiso skill search" in prompt.lower() or "never for plugin discovery" in prompt.lower()

    def test_m4_skill_reuse_rule(self):
        """M4: planner must use listed skills directly without re-verification."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Tools efficiency" in prompt or "Tools section" in prompt
        assert "confirmed installed" in prompt or "already-listed" in prompt

    def test_m4_no_reinstall_listed_skills(self):
        """M4: planner must not reinstall skills already in Skills section."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "no verification or reinstall needed" in prompt or "no verification needed" in prompt

    def test_m4_replan_no_reverify(self):
        """M4: planner must build on confirmed facts, not re-verify."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "confirmed facts" in prompt.lower() or "confirmed fact" in prompt.lower()
        assert "never re-investigate" in prompt.lower() or "re-verify" in prompt.lower()

    def test_m5_env_var_rule(self):
        """M5: planner must only ask for env vars declared in [kiso.env]."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "[kiso.env]" in prompt
        assert "absent or empty" in prompt or "If absent, proceed" in prompt

    def test_m5_msg_after_tasks(self):
        """M5/M137: msg tasks must come after data-gathering tasks."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "msg tasks must come after" in prompt

    def test_m142_file_based_data_flow(self):
        """M142: planner prompt requires file-based data flow for large outputs."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "save to file" in prompt
        assert "never embed raw data in details" in prompt.lower() or "never embed commands or raw data" in prompt.lower()

    def test_m143_strategy_diversification(self):
        """M143: planner must diversify strategy after repeated failures."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "fundamentally different strategy" in prompt
        assert "try a fundamentally different strategy" in prompt

    def test_m144_detail_natural_language(self):
        """M144: planner prompt forbids commands/data in detail field."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "never embed commands" in prompt.lower()

    def test_m144_long_exec_detail_rejected(self):
        """M144: exec task with >500 char detail is rejected."""
        plan = {"tasks": [
            {"type": "exec", "detail": "x" * 501, "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("too long" in e for e in errors)

    def test_m144_short_exec_detail_valid(self):
        """M144: exec task with <=500 char detail is fine."""
        plan = {"tasks": [
            {"type": "exec", "detail": "x" * 500, "expect": "ok"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("too long" in e for e in errors)

    def test_m352_no_kiso_instance_command(self):
        """M352: planner prompt must NOT reference nonexistent kiso instance command."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "kiso instance" not in prompt

    def test_m353_planner_self_identity(self):
        """M353: planner prompt declares 'You ARE Kiso'."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "You ARE Kiso" in prompt

    def test_m353_planner_self_entity(self):
        """M353: planner prompt references entity 'self' for knowledge base."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert 'entity "self"' in prompt or "Entity \"self\"" in prompt

    def test_m353_planner_no_kiso_cli_for_self_inspection(self):
        """M353: planner prompt forbids kiso CLI for self-inspection."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Do not use kiso CLI for self-inspection" in prompt

    def test_m423_answer_in_lang_once_in_planning_rules(self):
        """M423: 'Answer in {lang' appears once in planning_rules module."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # Extract planning_rules module content between markers
        start = prompt.index("<!-- MODULE: planning_rules -->")
        end = prompt.index("<!-- MODULE:", start + 1)
        planning_rules_text = prompt[start:end]
        assert planning_rules_text.count("Answer in {lang") == 1

    def test_m423_core_module_no_verbose_args(self):
        """M423: core module skill type doesn't mention 'NEVER null'."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        start = prompt.index("<!-- MODULE: core -->")
        end = prompt.index("<!-- MODULE:", start + 1)
        core_text = prompt[start:end]
        assert "NEVER null" not in core_text

    def test_m423_all_task_types_present(self):
        """M423: planner prompt still contains all task types."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        for tt in ("exec:", "tool:", "msg:", "search:", "replan:"):
            assert tt in prompt, f"Missing task type: {tt}"


class TestM165SkillArgsExample:
    """M165: planner prompt must include a tool args example."""

    def test_planner_prompt_has_skill_args_example(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "JSON string" in prompt
        assert "required args" in prompt

    def test_planner_prompt_no_hardcoded_browser_example(self):
        """M181: skill example must not hardcode 'browser' as skill name."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # The example line itself should use a generic name
        for line in prompt.splitlines():
            if line.strip().startswith("Example:") and '"skill":' in line:
                assert '"browser"' not in line, "Skill example should use generic name, not 'browser'"


class TestM180BrokenSkillRecoveryGuidance:
    """M180: planner prompt includes broken tool recovery guidance."""

    def test_planner_prompt_has_broken_tool_guidance(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Broken tool deps" in prompt
        assert "kiso tool remove" in prompt
        assert "kiso tool install" in prompt
        assert "[BROKEN]" in prompt


class TestM185BlockAptGetForToolDeps:
    """M185: planner prompt blocks manual apt-get for tool deps."""

    def test_planner_prompt_blocks_apt_get(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "NEVER use `apt-get install`" in prompt or "Never jump to `apt-get install`" in prompt

    def test_planner_prompt_blocks_pip_install(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "pip install" in prompt

    def test_reinstall_is_only_correct_fix(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "kiso tool remove NAME && kiso tool install NAME" in prompt


class TestM192PlannerNavigateAndInstallGuard:
    """M192: planner prompt has navigate-vs-search rule and install-then-replan guard."""

    def test_navigate_requires_browser_skill(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "requires `browser` tool" in prompt

    def test_search_not_for_page_interaction(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Do NOT use search for interaction" in prompt or "Interact" in prompt

    def test_cannot_use_uninstalled_skill(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "cannot be used" in prompt

    def test_install_then_replan_pattern(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Never tool-task an uninstalled tool" in prompt


class TestM207CompositeRequestDecomposition:
    """M207: planner decomposes composite requests by tool."""

    def test_composite_requests_rule_present(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Composite requests" in prompt

    def test_composite_requests_no_extra_steps(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "no extra steps" in prompt.lower() or "decompose per sub-goal" in prompt.lower()


class TestM199PluginInstallIdempotent:
    """M199: planner-plugin-install.md tells planner that install is idempotent."""

    def test_idempotent_note_present(self):
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "idempotent" in prompt

    def test_no_need_to_check_first(self):
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "no need to check" in prompt.lower() or "idempotent" in prompt.lower()


class TestM166ValidatePlanSkillArgs:
    """M166: validate_plan checks tool args against schema."""

    def test_missing_required_arg_rejected(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "screenshot", "skill": "browser",
             "args": "{}", "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        info = {"browser": {"args_schema": {"action": {"type": "string", "required": True}}}}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert any("missing required arg: action" in e for e in errors)

    def test_valid_args_accepted(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "screenshot", "skill": "browser",
             "args": '{"action": "screenshot"}', "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        info = {"browser": {"args_schema": {"action": {"type": "string", "required": True}}}}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert not errors

    def test_invalid_json_args_rejected(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "screenshot", "skill": "browser",
             "args": "not-json{", "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        info = {"browser": {"args_schema": {"action": {"type": "string", "required": True}}}}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert any("not valid JSON" in e for e in errors)

    def test_null_args_checked_against_schema(self):
        plan = {"tasks": [
            {"type": "skill", "detail": "screenshot", "skill": "browser",
             "args": None, "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        info = {"browser": {"args_schema": {"action": {"type": "string", "required": True}}}}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert any("missing required arg: action" in e for e in errors)

    def test_no_info_skips_args_validation(self):
        """When installed_skills_info is not provided, args are not validated."""
        plan = {"tasks": [
            {"type": "skill", "detail": "screenshot", "skill": "browser",
             "args": None, "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan, installed_skills=["browser"])
        assert not errors


    def test_m184_args_example_in_validation_error(self):
        """M184: validation error includes args example from schema."""
        plan = {"tasks": [
            {"type": "skill", "detail": "do stuff", "skill": "browser",
             "args": None, "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        info = {"browser": {"args_schema": {"action": {"type": "string", "required": True}}}}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert len(errors) == 1
        assert 'Set args to a JSON string like:' in errors[0]
        assert '"action": "value"' in errors[0]

    def test_m184_args_example_multiple_params(self):
        """M184: example includes all params from schema."""
        plan = {"tasks": [
            {"type": "skill", "detail": "do stuff", "skill": "browser",
             "args": "{}", "expect": "done"},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        info = {"browser": {"args_schema": {
            "action": {"type": "string", "required": True},
            "count": {"type": "int", "required": False},
        }}}
        errors = validate_plan(plan, installed_skills=["browser"],
                               installed_skills_info=info)
        assert len(errors) == 1
        assert '"action": "value"' in errors[0]
        assert '"count": 1' in errors[0]


class TestM171StripExtendReplan:
    """M171: strip extend_replan from initial plan."""

    def test_extend_replan_stripped_from_initial_plan(self):
        plan = {
            "extend_replan": 3,
            "tasks": [
                {"type": "msg", "detail": "hello", "expect": None, "skill": None, "args": None},
            ],
        }
        errors = validate_plan(plan, is_replan=False)
        assert not errors
        assert "extend_replan" not in plan

    def test_extend_replan_preserved_on_replan(self):
        plan = {
            "extend_replan": 2,
            "tasks": [
                {"type": "msg", "detail": "hello", "expect": None, "skill": None, "args": None},
            ],
        }
        errors = validate_plan(plan, is_replan=True)
        assert not errors
        assert plan.get("extend_replan") == 2

    def test_no_extend_replan_no_error(self):
        plan = {
            "tasks": [
                {"type": "msg", "detail": "hello", "expect": None, "skill": None, "args": None},
            ],
        }
        errors = validate_plan(plan, is_replan=False)
        assert not errors


class TestM73cPlannerUserManagement:
    """M73c: planner prompt rules for kiso user subcommand (now in appendix files)."""

    def test_kiso_user_commands_listed(self):
        """kiso user commands must appear in the kiso-commands appendix."""
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        assert "kiso user add" in prompt
        assert "kiso user remove|list" in prompt
        assert "kiso user alias" in prompt

    def test_user_management_admin_only_label(self):
        """The user commands must be labelled as admin only."""
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        idx = prompt.index("kiso user add")
        section = prompt[max(0, idx - 100):idx + 50]
        assert "admin only" in section

    def test_protection_rule_blocks_non_admin(self):
        """Planner user-mgmt appendix must refuse kiso user tasks when Caller Role is 'user'."""
        prompt = _load_modular_prompt("planner", ["user_mgmt"])
        assert "Caller Role" in prompt
        assert "never" in prompt.lower()
        assert "kiso user" in prompt

    def test_tools_required_for_role_user(self):
        """User-mgmt appendix must document that --tools is required when role=user."""
        prompt = _load_modular_prompt("planner", ["user_mgmt"])
        assert "--tools" in prompt
        assert "--role user" in prompt

    def test_ask_for_role_before_add(self):
        """Planner user-mgmt appendix must ask for role if not specified."""
        prompt = _load_modular_prompt("planner", ["user_mgmt"])
        assert "role is not specified" in prompt or "role not specified" in prompt or \
               "role" in prompt and "msg task" in prompt

    def test_ask_for_connector_aliases(self):
        """Planner user-mgmt + kiso_commands must mention connector aliases."""
        prompt = _load_modular_prompt("planner", ["user_mgmt", "kiso_commands"])
        assert "connector" in prompt.lower()
        assert "alias" in prompt.lower()


# --- M82: planner ask-then-add workflow (functional) ---


_MSG_PLAN_FOR_USER = json.dumps({
    "goal": "Relay user management request to admin",
    "secrets": None,
    "extend_replan": None,
    "tasks": [{
        "type": "msg",
        "detail": "I cannot add users directly. Please ask your admin to run: kiso user add bob --role user",
        "skill": None,
        "args": None,
        "expect": None,
    }],
})


@pytest.mark.asyncio
class TestM82PlannerAskThenAdd:
    """M82: functional tests for the ask-then-add protection workflow.

    When Caller Role=user, a kiso user management request must result in a
    msg task (not exec).  The protection is prompt-based; these tests verify:
    1. build_planner_messages correctly injects Caller Role: user into context.
    2. run_planner accepts a msg-only plan returned by the LLM for this scenario.
    3. The LLM actually sees the Caller Role and the original request together.
    """

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3, context_messages=5),
            raw={},
        )

    async def test_caller_role_user_in_messages(self, db, config):
        """build_planner_messages injects '## Caller Role\\nuser' for role=user."""
        msgs, *_ = await build_planner_messages(db, config, "sess1", "user", "add user bob")
        assert "## Caller Role\nuser" in msgs[1]["content"]

    async def test_run_planner_accepts_msg_only_plan(self, db, config):
        """run_planner with user_role='user' returns the msg plan without errors."""
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=_MSG_PLAN_FOR_USER):
            plan = await run_planner(db, config, "sess1", "user", "add user bob to kiso")
        assert plan["tasks"][0]["type"] == "msg"
        assert len(plan["tasks"]) == 1

    async def test_llm_sees_caller_role_and_request_together(self, db, config):
        """LLM receives both '## Caller Role\\nuser' and the kiso user add request."""
        captured: list[dict] = []

        async def _capture(cfg, role, messages, **kw):
            captured.extend(messages)
            return _MSG_PLAN_FOR_USER

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_planner(db, config, "sess1", "user", "kiso user add charlie")

        user_msg = next((m for m in captured if m["role"] == "user"), None)
        assert user_msg is not None, "No user message found in LLM call"
        assert "## Caller Role\nuser" in user_msg["content"]
        assert "kiso user add charlie" in user_msg["content"]

    async def test_on_context_ready_called_before_planner_llm(self, db, config):
        """on_context_ready fires after briefer/context but before planner LLM call."""
        call_order: list[str] = []

        async def _on_ready():
            call_order.append("context_ready")

        async def _fake_llm(cfg, role, messages, **kw):
            call_order.append(f"llm:{role}")
            return _MSG_PLAN_FOR_USER

        with patch("kiso.brain.call_llm", side_effect=_fake_llm):
            await run_planner(
                db, config, "sess1", "user", "hello",
                on_context_ready=_on_ready,
            )

        assert "context_ready" in call_order
        planner_idx = next(i for i, v in enumerate(call_order) if v == "llm:planner")
        ready_idx = call_order.index("context_ready")
        assert ready_idx < planner_idx, "on_context_ready must fire before planner LLM call"

    async def test_on_context_ready_none_is_noop(self, db, config):
        """on_context_ready=None (default) does not break anything."""
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=_MSG_PLAN_FOR_USER):
            plan = await run_planner(db, config, "sess1", "user", "hello")
        assert plan["goal"]


# --- Classifier (fast path) ---


def _make_config_for_classifier():
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=_full_models(worker="gpt-3.5"),
        settings=_full_settings(),
        raw={},
    )


class TestBuildClassifierMessages:
    def test_basic_structure(self):
        """build_classifier_messages returns system + user messages."""
        msgs = build_classifier_messages("hello there")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "hello there"

    def test_system_prompt_loaded(self):
        """System prompt should come from classifier.md."""
        msgs = build_classifier_messages("test")
        assert "plan" in msgs[0]["content"]
        assert "chat" in msgs[0]["content"]


class TestClassifyMessage:
    async def test_returns_chat(self):
        """classify_message returns 'chat' when LLM says 'chat'."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="chat"):
            result = await classify_message(config, "hello")
        assert result == "chat"

    async def test_returns_chat_kb(self):
        """M364: classify_message returns 'chat_kb' for knowledge-enriched chat."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="chat_kb"):
            result = await classify_message(config, "cosa sai su te stesso?")
        assert result == "chat_kb"

    async def test_returns_plan(self):
        """classify_message returns 'plan' when LLM says 'plan'."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="plan"):
            result = await classify_message(config, "list files")
        assert result == "plan"

    async def test_strips_whitespace(self):
        """classify_message handles LLM output with whitespace."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="  chat\n"):
            result = await classify_message(config, "thanks")
        assert result == "chat"

    async def test_case_insensitive(self):
        """classify_message handles uppercase responses."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="CHAT"):
            result = await classify_message(config, "thanks")
        assert result == "chat"

    async def test_unexpected_output_falls_back_to_plan(self):
        """classify_message returns 'plan' for unexpected LLM output."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="I think this is a chat"):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_llm_error_falls_back_to_plan(self):
        """classify_message returns 'plan' when LLM call fails."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, side_effect=LLMError("timeout")):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_budget_exceeded_falls_back_to_plan(self):
        """classify_message returns 'plan' when LLM budget is exhausted."""
        from kiso.llm import LLMBudgetExceeded
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, side_effect=LLMBudgetExceeded("over")):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_empty_response_falls_back_to_plan(self):
        """classify_message returns 'plan' when LLM returns empty string."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=""):
            result = await classify_message(config, "hello")
        assert result == "plan"

    async def test_uses_classifier_model(self):
        """classify_message should call LLM with 'classifier' role."""
        config = _make_config_for_classifier()
        mock_llm = AsyncMock(return_value="chat")
        with patch("kiso.brain.call_llm", mock_llm):
            await classify_message(config, "hello", session="s1")
        mock_llm.assert_called_once()
        assert mock_llm.call_args[0][1] == "classifier"  # role argument
        assert mock_llm.call_args[1].get("session") == "s1"


class TestClassifierPromptContent:
    def test_classifier_prompt_exists(self):
        """classifier.md role file should exist."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert len(prompt) > 0

    def test_classifier_prompt_mentions_categories(self):
        """Classifier prompt should define plan, chat_kb, and chat categories."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert "plan" in prompt
        assert "chat_kb" in prompt
        assert "chat" in prompt

    def test_classifier_prompt_safe_fallback(self):
        """Classifier prompt should instruct to default to plan when in doubt."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert "doubt" in prompt.lower()
        assert "plan" in prompt

    def test_classifier_prompt_covers_urls(self):
        """Classifier prompt should explicitly mention URLs/websites as 'plan' (M230)."""
        prompt = (_ROLES_DIR / "classifier.md").read_text().lower()
        assert "url" in prompt or "website" in prompt
        assert "domain" in prompt

    def test_classifier_prompt_covers_imperative_any_language(self):
        """Classifier prompt should mention action commands in any language (M230)."""
        prompt = (_ROLES_DIR / "classifier.md").read_text().lower()
        assert "any language" in prompt
        assert "imperative" in prompt

    def test_classifier_prompt_has_recent_context_rule(self):
        """M276: classifier prompt accepts Recent Context for follow-up detection."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert "Recent Context" in prompt
        assert "follow-up" in prompt.lower() or "follow up" in prompt.lower()

    def test_classifier_prompt_covers_system_introspection(self):
        """M350: classifier should route system self-inspection queries to plan."""
        prompt = (_ROLES_DIR / "classifier.md").read_text().lower()
        assert "system introspection" in prompt
        assert "ssh key" in prompt or "ssh keys" in prompt

    def test_classifier_prompt_defines_chat_kb(self):
        """M364: classifier prompt defines chat_kb category."""
        prompt = (_ROLES_DIR / "classifier.md").read_text()
        assert "chat_kb" in prompt

    def test_classifier_prompt_chat_kb_self_referential(self):
        """M364: chat_kb covers self-referential knowledge queries."""
        prompt = (_ROLES_DIR / "classifier.md").read_text().lower()
        assert "what do you know" in prompt
        assert "cosa sai" in prompt

    def test_classifier_prompt_chat_kb_entities(self):
        """M364: chat_kb covers questions about known entities."""
        prompt = (_ROLES_DIR / "classifier.md").read_text().lower()
        assert "entities" in prompt

    def test_classifier_categories_constant(self):
        """M364: CLASSIFIER_CATEGORIES includes plan, chat, and chat_kb."""
        assert "plan" in CLASSIFIER_CATEGORIES
        assert "chat" in CLASSIFIER_CATEGORIES
        assert "chat_kb" in CLASSIFIER_CATEGORIES


class TestM276ClassifierContext:
    """M276: classifier receives conversation context for follow-up detection."""

    def test_build_messages_without_context(self):
        msgs = build_classifier_messages("hello")
        assert "Recent Context" not in msgs[1]["content"]

    def test_build_messages_with_context(self):
        msgs = build_classifier_messages("e la pagina?", recent_context="Last plan goal: Navigate to example.com")
        assert "Recent Context" in msgs[1]["content"]
        assert "Navigate to example.com" in msgs[1]["content"]

    def test_build_messages_context_appended_after_content(self):
        msgs = build_classifier_messages("test msg", recent_context="Last plan goal: X")
        user_content = msgs[1]["content"]
        # Content comes first, context after
        assert user_content.index("test msg") < user_content.index("Recent Context")

    async def test_classify_passes_context_to_llm(self):
        config = _make_config_for_classifier()
        mock_llm = AsyncMock(return_value="plan")
        with patch("kiso.brain.call_llm", mock_llm):
            await classify_message(config, "e la pagina?", recent_context="Last plan goal: Nav")
        # Check the user message includes context
        messages = mock_llm.call_args[0][2]
        assert "Recent Context" in messages[1]["content"]

    async def test_classify_empty_context_no_section(self):
        config = _make_config_for_classifier()
        mock_llm = AsyncMock(return_value="chat")
        with patch("kiso.brain.call_llm", mock_llm):
            await classify_message(config, "hello", recent_context="")
        messages = mock_llm.call_args[0][2]
        assert "Recent Context" not in messages[1]["content"]


# --- M234: Planner — don't decompose atomic CLI operations ---


class TestM234PlannerAtomicOperations:
    """M234: planner prompt tells LLM not to decompose atomic CLI commands."""

    def test_planner_prompt_atomic_operations_rule(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "atomic" in prompt.lower()
        assert "kiso tool install" in prompt or "Install commands are atomic" in prompt
        assert "never decompose" in prompt.lower()

    def test_planner_prompt_atomic_covers_package_managers(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # Compressed prompt uses generic "Install commands are atomic" instead of listing each manager
        assert "atomic" in prompt.lower()
        assert "never decompose" in prompt.lower() or "single command" in prompt.lower()


# --- M275: Planner — enforce skill usage_guide compliance ---


class TestM275PlannerUsageGuideCompliance:
    """M275: planner prompt tells the model to follow skill usage guides."""

    def test_planner_prompt_usage_guide_rule(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "usage guide" in prompt.lower() or "guide:" in prompt
        assert "follow" in prompt.lower()
        # Must be in the tools_rules module
        assert "guide:" in prompt

    def test_planner_prompt_usage_guide_is_mandatory(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "strictly" in prompt.lower() or "broken plans" in prompt.lower()


# --- M235: Planner — scope limited to current user request ---


class TestM286PlannerLanguageUniversal:
    """M286: planner handles any input language without bias."""

    def test_planner_any_language_any_script(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "any language" in prompt
        assert "any script" in prompt

    def test_planner_language_handling_rule(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Msg detail:" in prompt
        assert "messenger translates" in prompt.lower()


class TestM235PlannerScope:
    """M235: planner prompt explicitly limits scope to current message."""

    def test_planner_prompt_no_carry_forward(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Do NOT carry forward objectives" in prompt or "Plan ONLY what the New Message asks" in prompt

    def test_planner_prompt_replan_not_for_history(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "background context only" in prompt.lower()


# --- M33: retry_hint in REVIEW_SCHEMA ---


class TestRetryHintInSchema:
    def test_retry_hint_in_review_schema(self):
        """REVIEW_SCHEMA includes retry_hint property."""
        props = REVIEW_SCHEMA["json_schema"]["schema"]["properties"]
        assert "retry_hint" in props
        required = REVIEW_SCHEMA["json_schema"]["schema"]["required"]
        assert "retry_hint" in required

    def test_validate_review_ok_with_retry_hint(self):
        review = {"status": "ok", "reason": None, "learn": None, "retry_hint": None}
        assert validate_review(review) == []

    def test_validate_review_replan_with_retry_hint(self):
        review = {"status": "replan", "reason": "wrong path", "learn": None, "retry_hint": "use /opt/app"}
        assert validate_review(review) == []

    async def test_run_reviewer_returns_retry_hint(self):
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(reviewer="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )
        review_json = json.dumps({
            "status": "replan", "reason": "wrong path",
            "learn": None, "retry_hint": "use /opt/app",
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=review_json):
            review = await run_reviewer(config, "goal", "detail", "expect", "output", "msg")
        assert review["retry_hint"] == "use /opt/app"


# --- M33: retry_context in exec translator ---


class TestExecTranslatorRetryContext:
    def test_retry_context_included_in_messages(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List Python files", "OS: Linux",
            retry_context="Previous command failed. Hint: use python3 not python",
        )
        content = msgs[1]["content"]
        assert "## Retry Context" in content
        assert "use python3 not python" in content

    def test_retry_context_empty_not_included(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List Python files", "OS: Linux",
            retry_context="",
        )
        content = msgs[1]["content"]
        assert "Retry Context" not in content

    def test_retry_context_before_task_section(self):
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config, "List Python files", "OS: Linux",
            retry_context="Hint: use python3",
        )
        content = msgs[1]["content"]
        retry_pos = content.index("## Retry Context")
        task_pos = content.index("## Task")
        assert retry_pos < task_pos

    async def test_run_exec_translator_passes_retry_context(self):
        config = _make_brain_config(models=_full_models(worker="gpt-4"))
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "python3 script.py"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            result = await run_exec_translator(
                config, "Run script", "OS: Linux",
                retry_context="use python3 not python",
            )
        assert result == "python3 script.py"
        user_content = captured_messages[1]["content"]
        assert "## Retry Context" in user_content
        assert "use python3 not python" in user_content


# --- M33: reviewer prompt mentions retry_hint ---


class TestReviewerPromptRetryHint:
    def test_reviewer_prompt_mentions_retry_hint(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "retry_hint" in prompt

    def test_m206_reviewer_partial_success_guidance(self):
        """M206: reviewer prompt guides summary to cover partial successes."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "partial successes" in prompt or "Partial success" in prompt


# --- M47: planner/reviewer/worker improvements ---


class TestM47PlannerIdentityAndTwoLayer:
    """47a: planner self-awareness — identity + two-layer environment."""

    def test_planner_prompt_mentions_kiso_identity(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Kiso planner" in prompt

    def test_planner_prompt_mentions_two_layers(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "two layers" in prompt or "Docker container" in prompt

    def test_planner_prompt_prefers_kiso_native_solution(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Kiso-native" in prompt

    def test_planner_prompt_unambiguous_bias(self):
        """Clarification rule flipped: proceed only if unambiguous, else ask."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "unambiguous" in prompt or "unclear" in prompt
        assert "When in doubt, ask" in prompt or "asking for clarification" in prompt

    def test_planner_prompt_expect_scoping(self):
        """expect must be task-scoped, not plan-goal-scoped."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "THIS task's output" in prompt
        assert "not the overall goal" in prompt or "not overall goal" in prompt


class TestM47ReviewerPlanContext:
    """47b: reviewer receives Plan Context (not Plan Goal) and evaluates only expect."""

    async def test_reviewer_messages_use_plan_context_label(self):
        msgs = build_reviewer_messages(
            goal="Install Discord",
            detail="run apt-get install -f",
            expect="exits 0",
            output="0 upgraded, 0 installed",
            user_message="install discord",
            success=True,
        )
        content = msgs[1]["content"]
        assert "## Plan Context" in content
        assert "## Plan Goal" not in content

    def test_reviewer_prompt_plan_context_is_background(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "background" in prompt and "context" in prompt.lower()

    def test_reviewer_prompt_sole_criterion_is_expect(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "Sole criterion" in prompt

    def test_reviewer_prompt_zero_changes_is_success(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "0 changes" in prompt or "nothing to do" in prompt


# --- M96: reviewer — warning vs error distinction ---


class TestReviewerWarningVsError:
    """M96: warnings don't override exit 0 + satisfied expect."""

    def test_prompt_warnings_are_informational(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "informational" in prompt or "don't override exit 0" in prompt.lower()

    def test_prompt_no_blanket_replan_on_warning(self):
        """Old rule 'mark as replan even if command succeeded' must be gone."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "mark as replan even if the command succeeded" not in prompt

    def test_prompt_explicit_expect_required_for_warning_replan(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "expect" in prompt.lower()
        # The new rule: replan for warnings only if expect requires clean output
        assert "absence of warnings" in prompt or "no warnings" in prompt


class TestPrepareReviewerOutput:
    """M224: reviewer output preparation with tail + error grep + stderr."""

    def test_small_output_passthrough(self):
        """Output under limit is returned unchanged."""
        from kiso.brain import prepare_reviewer_output
        stdout = "hello world\nexit 0"
        stderr = ""
        result = prepare_reviewer_output(stdout, stderr)
        assert result == "hello world\nexit 0"

    def test_small_output_with_stderr_passthrough(self):
        """Small combined output (stdout + stderr) returned as-is."""
        from kiso.brain import prepare_reviewer_output
        stdout = "line1\nline2"
        stderr = "warning: foo"
        result = prepare_reviewer_output(stdout, stderr)
        assert "line1" in result
        assert "warning: foo" in result

    def test_large_output_truncated(self):
        """100K stdout is truncated to ≤ limit."""
        from kiso.brain import prepare_reviewer_output
        stdout = "\n".join(f"line {i}: ok" for i in range(5000))
        result = prepare_reviewer_output(stdout, "", limit=4000)
        assert len(result) <= 4000
        assert "OUTPUT TRUNCATED" in result

    def test_error_in_middle_captured(self):
        """Error line buried in the middle of large output appears in grep section."""
        from kiso.brain import prepare_reviewer_output
        lines = [f"line {i}: ok" for i in range(500)]
        lines[50] = "FATAL error: disk full"
        stdout = "\n".join(lines)
        result = prepare_reviewer_output(stdout, "", limit=4000)
        assert "FATAL error: disk full" in result
        assert "error matches" in result

    def test_tail_present(self):
        """Last lines of stdout appear in the tail section."""
        from kiso.brain import prepare_reviewer_output
        lines = [f"line {i}" for i in range(500)]
        lines[-1] = "BUILD SUCCESS"
        stdout = "\n".join(lines)
        result = prepare_reviewer_output(stdout, "", limit=4000)
        assert "BUILD SUCCESS" in result
        assert "last" in result

    def test_stderr_section_present(self):
        """Non-empty stderr gets its own section."""
        from kiso.brain import prepare_reviewer_output
        stdout = "\n".join(f"line {i}" for i in range(500))
        stderr = "error: something failed\ndetails: bad input"
        result = prepare_reviewer_output(stdout, stderr, limit=4000)
        assert "--- stderr" in result
        assert "something failed" in result

    def test_grep_dedup_with_tail(self):
        """Error lines already in tail are not duplicated in grep section."""
        from kiso.brain import prepare_reviewer_output
        lines = [f"line {i}: ok" for i in range(200)]
        lines[-5] = "error: final issue"  # this is in the tail
        stdout = "\n".join(lines)
        result = prepare_reviewer_output(stdout, "", limit=4000)
        # "error: final issue" should appear once (in tail) but NOT in grep matches
        # (because it's already in the tail set)
        count = result.count("error: final issue")
        assert count == 1

    def test_empty_output(self):
        """Empty stdout and stderr returns empty string."""
        from kiso.brain import prepare_reviewer_output
        result = prepare_reviewer_output("", "")
        assert result == ""

    def test_budget_priority_stderr_preserved(self):
        """Even with huge stdout, stderr is preserved."""
        from kiso.brain import prepare_reviewer_output
        stdout = "x" * 100000
        stderr = "critical error\n"
        result = prepare_reviewer_output(stdout, stderr, limit=4000)
        assert "critical error" in result


class TestM47WorkerHintPriority:
    """47d: worker gives priority to retry hint over literal task detail re-translation."""

    def test_worker_prompt_hint_takes_priority(self):
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "hint" in prompt.lower()
        assert "ABSOLUTE priority" in prompt

    def test_m284_tool_path_awareness(self):
        """M284: worker prompt mentions tool venv PATH."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "Tool binaries" in prompt or "tool venv PATH" in prompt

    def test_retry_context_with_hint_is_visible_to_translator(self):
        """Hint in retry context is present in the messages sent to the exec translator."""
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config,
            detail="Fix remaining dependency issues with apt-get install -f",
            sys_env_text="OS: Linux",
            retry_context="Attempt 1 failed.\nHint: use apt install discord instead",
        )
        content = msgs[1]["content"]
        assert "## Retry Context" in content
        assert "apt install discord" in content

    def test_retry_context_without_hint_still_works(self):
        """Retry context without hint does not break translation."""
        config = _make_brain_config()
        msgs = build_exec_translator_messages(
            config,
            detail="list files",
            sys_env_text="OS: Linux",
            retry_context="Attempt 1 failed. Command not found.",
        )
        content = msgs[1]["content"]
        assert "## Retry Context" in content
        assert "## Task" in content


# --- M47: edge cases ---


class TestM47ReviewerPlanContextEdgeCases:
    """Edge cases for reviewer Plan Context / expect evaluation."""

    async def test_reviewer_messages_goal_text_present_as_context(self):
        """Goal text is still present in the message — just under Plan Context label."""
        msgs = build_reviewer_messages(
            goal="Install Discord on the system",
            detail="run apt-get install -f",
            expect="exits 0, no broken dependencies (0 changes acceptable)",
            output="0 upgraded, 0 installed, 0 to remove",
            user_message="install discord",
            success=True,
        )
        content = msgs[1]["content"]
        # Goal text is present for context
        assert "Install Discord on the system" in content
        # But under the right label
        assert "## Plan Context" in content

    async def test_reviewer_messages_structure_order(self):
        """Plan Context comes before Task Detail which comes before Expected Outcome."""
        msgs = build_reviewer_messages(
            goal="some goal",
            detail="some detail",
            expect="some expect",
            output="some output",
            user_message="user msg",
        )
        content = msgs[1]["content"]
        plan_ctx_pos = content.index("## Plan Context")
        task_detail_pos = content.index("## Task Detail")
        expected_pos = content.index("## Expected Outcome")
        assert plan_ctx_pos < task_detail_pos < expected_pos

    async def test_reviewer_messages_no_success_flag(self):
        """build_reviewer_messages works without success flag (no Command Status section)."""
        msgs = build_reviewer_messages(
            goal="goal",
            detail="detail",
            expect="expect",
            output="output",
            user_message="msg",
        )
        content = msgs[1]["content"]
        assert "## Plan Context" in content
        assert "Command Status" not in content

    async def test_reviewer_messages_with_success_false(self):
        """Failed exit code is reported correctly."""
        msgs = build_reviewer_messages(
            goal="goal",
            detail="detail",
            expect="expect",
            output="error output",
            user_message="msg",
            success=False,
        )
        content = msgs[1]["content"]
        assert "FAILED" in content or "non-zero" in content


class TestM47PlannerExpectScopingEdgeCases:
    """Edge cases for planner expect scoping guidance."""

    def test_planner_prompt_maintenance_commands_mentioned(self):
        """Prompt mentions apt-get install as a known command pattern."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "apt-get install" in prompt

    def test_planner_prompt_clarification_rule_not_just_unclear(self):
        """New rule is more than 'if unclear, ask' — requires unambiguous intent AND target."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # Old rule was simply "if the request is unclear"
        # New rule requires both intent and target to be unambiguous
        assert "intent" in prompt
        assert "target" in prompt or "clarification" in prompt


# ---------------------------------------------------------------------------
# M48: Prompt hygiene — ottimizzazioni trasversali ai role prompts
# ---------------------------------------------------------------------------


class TestM48ReviewerPromptHygiene:
    """48a+48b: reviewer prompt alignment and exit code rule."""

    def test_48a_you_receive_says_plan_context(self):
        """48a: reviewer prompt must refer to 'Plan Context', not 'Plan Goal'."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "Plan Context" in prompt

    def test_48a_no_plan_goal_in_receive_block(self):
        """48a: old 'The plan goal' bullet must be gone from 'You receive' section."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "The plan goal" not in prompt

    def test_48b_exit_code_rule_present(self):
        """48b: reviewer prompt must have an explicit rule about exit codes."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "exit code" in prompt.lower() or "non-zero" in prompt.lower()

    def test_48b_nonzero_is_failure_signal(self):
        """48b: non-zero exit code must be labeled as failure indicator."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "non-zero" in prompt.lower()

    def test_48b_zero_not_sufficient_alone(self):
        """48b: zero exit code is necessary but not sufficient — output must also satisfy expect."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "not sufficient" in prompt.lower() or "be strict" in prompt.lower()


class TestM354ReviewerLearnGuards:
    """M354: reviewer learn rules guard against false causal inferences."""

    def test_no_causal_inference_rule(self):
        """M354: reviewer must not infer causal relationships from single failures."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "causal inferences" in prompt.lower()

    def test_cli_usage_errors_rule(self):
        """M354: CLI usage errors should not become durable learnings."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "CLI usage errors" in prompt


class TestM48PlannerMergedRules:
    """48c: planner expect and detail rules are merged (no redundant duplicates)."""

    def test_48c_expect_rule_is_nonnull_and_task_scoped(self):
        """48c: single expect rule combines 'non-null' and 'task-specific' guidance."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "non-null" in prompt
        assert "not the overall goal" in prompt or "THIS task's output" in prompt

    def test_48c_no_redundant_standalone_nonnull_rule(self):
        """48c: old fragmented 'non-null expect field' standalone line must be gone."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "exec, skill, and search tasks MUST have a non-null expect field" not in prompt

    def test_48c_detail_rule_is_natural_language(self):
        """48c/M144: detail must be natural language, not commands/data."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "natural language" in prompt
        assert "never embed commands" in prompt.lower()

    def test_48c_no_redundant_standalone_specific_rule(self):
        """48c: old fragmented 'exec task detail must be specific' standalone line must be gone."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "exec task detail must be specific" not in prompt

    def test_48c_expect_rule_covers_exec_skill_search(self):
        """48c: merged expect rule must apply to exec/skill/search."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        # Should mention all three types together
        assert "exec/tool/search" in prompt or ("exec" in prompt and "tool" in prompt and "search" in prompt)

    def test_48c_detail_rule_mentions_exec_commands_and_paths(self):
        """48c: merged detail rule must mention commands/paths for exec tasks."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "commands" in prompt or "paths" in prompt


# --- M97a: planner contextual rule blocks ---


@pytest.mark.asyncio
class TestPlannerContextualRules:
    """M97a: appendix blocks injected only when message keywords match."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "test-session")
        yield conn
        await conn.close()

    def _config(self):
        return _make_brain_config()

    async def test_generic_message_has_no_appendix(self, db):
        """A message like 'what time is it' should not inject any appendix (when skills exist)."""
        fake_skills = [{"name": "browser", "version": "1.0", "summary": "Browse the web", "commands": {}}]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin", "what time is it",
            )
        system = msgs[0]["content"]
        # "Plugin installation:" is the appendix marker — should NOT be injected for generic messages
        assert "PROTECTION" not in system
        assert "Plugin installation:" not in system

    async def test_skill_keyword_injects_kiso_commands(self, db):
        """Message mentioning 'skill' should inject kiso-commands appendix."""
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "install the search skill",
        )
        system = msgs[0]["content"]
        assert "kiso tool install" in system

    async def test_user_keyword_injects_user_mgmt(self, db):
        """Message mentioning 'user' should inject user-mgmt appendix."""
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "add a new user bob",
        )
        system = msgs[0]["content"]
        assert "PROTECTION" in system or "Caller Role" in system

    async def test_install_keyword_injects_plugin_install(self, db):
        """Message mentioning 'install' should inject plugin-install appendix."""
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "install the browser connector",
        )
        system = msgs[0]["content"]
        assert "Plugin installation:" in system

    async def test_not_installed_in_replan_injects_plugin_install(self, db):
        """M123: replan context with 'not installed' should inject plugin-install appendix."""
        replan_msg = (
            "vorrei navigare su internet\n\n"
            "## Failure Reason\nskill 'browser' is not installed. Available skills: none"
        )
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin", replan_msg,
        )
        system = msgs[0]["content"]
        assert "Plugin installation:" in system

    async def test_registry_keyword_injects_plugin_install(self, db):
        """M123: message with 'registry' should inject plugin-install appendix."""
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin",
            "check the registry for browser skill",
        )
        system = msgs[0]["content"]
        assert "Plugin installation:" in system

    async def test_no_skills_injects_plugin_install(self, db):
        """M129: when no skills are installed, always inject plugin-install appendix."""
        with patch("kiso.brain.discover_tools", return_value=[]):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin", "what time is it",
            )
        system = msgs[0]["content"]
        assert "Plugin installation:" in system

    async def test_no_skills_no_duplicate_appendix(self, db):
        """M129: if keyword already triggered plugin-install, no duplicate on empty skills."""
        with patch("kiso.brain.discover_tools", return_value=[]):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin", "install the browser skill",
            )
        system = msgs[0]["content"]
        # Should appear exactly once
        assert system.count("Plugin installation:") == 1

    async def test_capability_gap_injects_plugin_install(self, db):
        """M153: message needing uninstalled capability triggers plugin-install appendix."""
        # "screenshot" maps to "browser" skill — inject appendix when browser not installed
        fake_skills = [{"name": "search", "version": "1.0", "summary": "Search", "commands": {}}]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "take a screenshot of example.com",
            )
        system = msgs[0]["content"]
        assert "Plugin installation:" in system

    async def test_capability_gap_no_inject_when_skill_installed(self, db):
        """M153: no plugin-install injection when the capability skill IS installed."""
        fake_skills = [
            {"name": "browser", "version": "1.0", "summary": "Browse", "commands": {}},
        ]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "take a screenshot of example.com",
            )
        system = msgs[0]["content"]
        assert "Plugin installation:" not in system

    async def test_capability_gap_exposes_skill_name_in_context(self, db):
        """M198: capability gap detection result is visible in planner context."""
        fake_skills = [{"name": "search", "version": "1.0", "summary": "Search", "commands": {}}]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "take a screenshot of the homepage",
            )
        context = msgs[1]["content"]
        assert "Capability Analysis" in context
        assert "browser" in context
        assert "kiso tool install browser" in context

    async def test_capability_gap_no_context_when_skill_installed(self, db):
        """M198: no capability analysis when the needed skill is installed."""
        fake_skills = [
            {"name": "browser", "version": "1.0", "summary": "Browse", "commands": {}},
        ]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "take a screenshot of the homepage",
            )
        context = msgs[1]["content"]
        assert "Capability Analysis" not in context

    async def test_capability_gap_with_no_skills_installed(self, db):
        """M198: capability analysis works even when no skills are installed."""
        with patch("kiso.brain.discover_tools", return_value=[]):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "take a screenshot please",
            )
        context = msgs[1]["content"]
        assert "Capability Analysis" in context
        assert "browser" in context

    async def test_capability_gap_no_trigger_on_generic_words(self, db):
        """M223: generic words like 'browse', 'form', 'click' don't trigger capability gap."""
        fake_skills = [{"name": "search", "version": "1.0", "summary": "Search", "commands": {}}]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "browse the web and fill the form",
            )
        context = msgs[1]["content"]
        assert "Capability Analysis" not in context

    async def test_base_prompt_always_present(self, db):
        """Core planner rules are always present regardless of message."""
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin", "hello",
        )
        system = msgs[0]["content"]
        assert "Kiso planner" in system
        assert "CRITICAL" in system


class TestM48CuratorCategoryField:
    """48d: curator category field — prompt, schema, and validation."""

    def test_48d_curator_prompt_mentions_category(self):
        """48d: curator prompt must instruct model to include category for promote."""
        prompt = (_ROLES_DIR / "curator.md").read_text()
        assert "category" in prompt

    def test_48d_curator_prompt_lists_all_valid_categories(self):
        """48d: curator prompt must list all valid category values."""
        prompt = (_ROLES_DIR / "curator.md").read_text()
        for cat in ("project", "user", "tool", "general"):
            assert cat in prompt

    def test_48d_curator_schema_has_category_field(self):
        """48d: CURATOR_SCHEMA item must include 'category' property."""
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        assert "category" in item_props

    def test_48d_curator_schema_category_is_nullable(self):
        """48d: category field must be nullable (anyOf [string|null])."""
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        cat = item_props["category"]
        types_in_anyof = [x.get("type") for x in cat.get("anyOf", [])]
        assert "null" in types_in_anyof

    def test_48d_curator_schema_category_enum_contains_all_values(self):
        """48d: category enum must contain project/user/tool/general."""
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        cat = item_props["category"]
        enum_values = [x.get("enum", []) for x in cat.get("anyOf", []) if x.get("type") == "string"]
        flat = [v for sub in enum_values for v in sub]
        for v in ("project", "user", "tool", "general"):
            assert v in flat

    def test_48d_curator_schema_category_in_required_list(self):
        """48d: category must be in the required field list of the item schema."""
        required = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["required"]
        assert "category" in required

    def test_48d_validate_curator_accepts_valid_category(self):
        """48d: validate_curator passes when promote has a valid category."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": "project", "question": None, "reason": "Good", "entity_name": "myproject", "entity_kind": "project"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_48d_validate_curator_accepts_null_category(self):
        """48d: validate_curator passes when category is null (defaults to general at runtime)."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": None, "question": None, "reason": "Good", "entity_name": "myproject", "entity_kind": "project"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_48d_validate_curator_rejects_invalid_category(self):
        """48d: validate_curator rejects unknown category string values."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python", "category": "invalid_cat", "question": None, "reason": "Good"},
        ]}
        errors = validate_curator(result)
        assert any("category" in e for e in errors)

    def test_m282_tag_reuse_rule(self):
        """M282: curator prompt enforces tag reuse over synonyms."""
        prompt = (_ROLES_DIR / "curator.md").read_text()
        assert "Tag reuse" in prompt
        assert "NEVER create a synonym" in prompt or "NEVER create synonym" in prompt

    def test_m282_contradiction_rule(self):
        """M282: curator prompt handles contradicting facts."""
        prompt = (_ROLES_DIR / "curator.md").read_text()
        assert "Contradicting facts" in prompt
        assert "newer takes precedence" in prompt.lower()

    def test_48d_validate_curator_ignores_category_for_ask(self):
        """48d: validate_curator does not enforce category for ask verdicts."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "ask", "fact": None, "category": None, "question": "Which DB?", "reason": "Unclear"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_48d_validate_curator_ignores_category_for_discard(self):
        """48d: validate_curator does not enforce category for discard verdicts."""
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "discard", "fact": None, "category": None, "question": None, "reason": "Transient"},
        ]}
        errors = validate_curator(result)
        assert errors == []


# --- M343: curator entity_name + entity_kind ---


class TestM343CuratorEntityFields:
    """M343: validate_curator enforces entity_name + entity_kind for promote."""

    def test_promote_missing_entity_name_error(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python framework", "category": "project",
             "question": None, "reason": "Good", "tags": ["tech-stack"],
             "entity_name": None, "entity_kind": "project"},
        ]}
        errors = validate_curator(result)
        assert any("entity_name" in e for e in errors)

    def test_promote_missing_entity_kind_error(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python framework", "category": "project",
             "question": None, "reason": "Good", "tags": ["tech-stack"],
             "entity_name": "myproject", "entity_kind": None},
        ]}
        errors = validate_curator(result)
        assert any("entity_kind" in e for e in errors)

    def test_promote_invalid_entity_kind_error(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python framework", "category": "project",
             "question": None, "reason": "Good", "tags": ["tech-stack"],
             "entity_name": "myproject", "entity_kind": "unknown_kind"},
        ]}
        errors = validate_curator(result)
        assert any("entity_kind" in e for e in errors)

    def test_discard_without_entity_ok(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "discard", "fact": None, "category": None,
             "question": None, "reason": "Transient"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_ask_without_entity_ok(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "ask", "fact": None, "category": None,
             "question": "Which database?", "reason": "Unclear"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_promote_with_valid_entity_ok(self):
        result = {"evaluations": [
            {"learning_id": 1, "verdict": "promote", "fact": "Uses Python framework", "category": "project",
             "question": None, "reason": "Good", "tags": ["tech-stack"],
             "entity_name": "myproject", "entity_kind": "project"},
        ]}
        errors = validate_curator(result)
        assert errors == []

    def test_schema_has_entity_fields(self):
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        assert "entity_name" in item_props
        assert "entity_kind" in item_props

    def test_schema_entity_kind_enum(self):
        item_props = CURATOR_SCHEMA["json_schema"]["schema"]["properties"]["evaluations"]["items"]["properties"]
        kind = item_props["entity_kind"]
        enum_values = [x.get("enum", []) for x in kind.get("anyOf", []) if x.get("type") == "string"]
        flat = [v for sub in enum_values for v in sub]
        for v in ("website", "company", "tool", "person", "project", "concept"):
            assert v in flat

    def test_build_curator_messages_with_entities(self):
        entities = [{"name": "flask", "kind": "tool"}, {"name": "myproject", "kind": "project"}]
        msgs = build_curator_messages(
            [{"id": 1, "content": "test"}],
            available_entities=entities,
        )
        user_content = msgs[1]["content"]
        assert "## Existing Entities" in user_content
        assert "flask" in user_content
        assert "myproject" in user_content

    def test_build_curator_messages_no_entities(self):
        msgs = build_curator_messages([{"id": 1, "content": "test"}])
        assert "## Existing Entities" not in msgs[1]["content"]


# --- M347: curator dedup — existing entity facts ---


class TestM347CuratorExistingFacts:
    """M347: build_curator_messages injects existing facts for dedup."""

    def test_existing_facts_section_injected(self):
        facts = [
            {"content": "guidance.studio has CAPTCHA", "entity_name": "guidance.studio"},
            {"content": "guidance.studio uses Webflow", "entity_name": "guidance.studio"},
        ]
        msgs = build_curator_messages(
            [{"id": 1, "content": "guidance.studio form has CAPTCHA"}],
            existing_facts=facts,
        )
        user_content = msgs[1]["content"]
        assert "## Existing Facts (already in knowledge base)" in user_content
        assert "guidance.studio has CAPTCHA" in user_content
        assert "[entity: guidance.studio]" in user_content

    def test_no_existing_facts_no_section(self):
        msgs = build_curator_messages([{"id": 1, "content": "test"}])
        assert "## Existing Facts" not in msgs[1]["content"]

    def test_existing_facts_empty_no_section(self):
        msgs = build_curator_messages(
            [{"id": 1, "content": "test"}], existing_facts=[],
        )
        assert "## Existing Facts" not in msgs[1]["content"]

    def test_curator_prompt_dedup_rule(self):
        prompt = _load_system_prompt("curator")
        assert "Existing Fact" in prompt
        assert "discard" in prompt.lower()

    async def test_run_curator_forwards_existing_facts(self):
        """M347: run_curator forwards existing_facts to build_curator_messages."""
        facts = [{"content": "Flask is used", "entity_name": "flask"}]
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(curator="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=VALID_CURATOR) as mock_llm:
            await run_curator(config, [{"id": 1, "content": "test"}], existing_facts=facts)
        messages = mock_llm.call_args[1].get("messages") or mock_llm.call_args[0][2]
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert "Flask is used" in user_msg["content"]
        assert "## Existing Facts" in user_msg["content"]


# --- M321: curator consolidation + learning_id rules ---


def test_curator_prompt_consolidation_rule():
    """M321: curator prompt instructs consolidation of duplicate learnings."""
    prompt = _load_system_prompt("curator")
    assert "consolidat" in prompt.lower()
    assert "ONE evaluation" in prompt


def test_curator_prompt_learning_id_rule():
    """M321: curator prompt explicitly mentions learning_id matching."""
    prompt = _load_system_prompt("curator")
    assert "learning_id" in prompt


def test_curator_prompt_discard_transient_examples():
    """M321: curator prompt lists concrete transient discard examples."""
    prompt = _load_system_prompt("curator")
    assert "loaded/installed successfully" in prompt or "installed successfully" in prompt


class TestM48SummarizerFactsTiebreaker:
    """48e: summarizer-facts tiebreaker rule for contradictions."""

    def test_48e_tiebreaker_rule_present(self):
        """48e: summarizer-facts prompt must mention contradiction resolution."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "contradict" in prompt.lower()

    def test_48e_tiebreaker_higher_confidence_wins(self):
        """48e: tiebreaker must prefer the higher-confidence fact."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "higher confidence" in prompt.lower()

    def test_48e_tiebreaker_specific_over_general(self):
        """48e: equal-confidence tiebreaker must prefer more specific fact."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "specific" in prompt.lower()


# --- M106: exit code notes, default model, prompt rules ---


class TestM106ExitCodeNotes:
    """M106c: _EXIT_CODE_NOTES dictionary completeness."""

    def test_notes_cover_expected_codes(self):
        from kiso.brain import _EXIT_CODE_NOTES
        assert 1 in _EXIT_CODE_NOTES
        assert 2 in _EXIT_CODE_NOTES
        assert 126 in _EXIT_CODE_NOTES
        assert 127 in _EXIT_CODE_NOTES
        assert -1 in _EXIT_CODE_NOTES

    def test_note_1_mentions_grep(self):
        from kiso.brain import _EXIT_CODE_NOTES
        assert "grep" in _EXIT_CODE_NOTES[1].lower()

    def test_note_127_mentions_not_found(self):
        from kiso.brain import _EXIT_CODE_NOTES
        assert "not found" in _EXIT_CODE_NOTES[127].lower()


class TestM106DefaultPlannerModel:
    """M110c: default planner model is deepseek-v3.2."""

    def test_default_planner_model(self):
        from kiso.config import MODEL_DEFAULTS
        assert MODEL_DEFAULTS["planner"] == "deepseek/deepseek-v3.2"


class TestM106PlannerKisoNativeFirst:
    """M106a: planner prompt must include Kiso-native-first rule."""

    def test_kiso_native_first_rule_present(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "kiso-native" in prompt.lower() or "kiso layer" in prompt.lower()

    def test_check_skills_before_os(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "apt-get" in prompt or "os-level" in prompt.lower()


class TestM106ReviewerExitCodeRules:
    """M106b: reviewer prompt covers verification task exit code semantics."""

    def test_verification_task_exit1_rule(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "nothing found" in prompt.lower() or "no matches" in prompt.lower()

    def test_anti_loop_rule(self):
        """Reviewer prompt must have anti-loop guidance."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "same output" in prompt.lower() or "retry" in prompt.lower()


class TestM6ReviewerSubstanceOverFormat:
    """M6: reviewer must accept correct check results regardless of output format."""

    def test_substance_over_format_rule(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "substance" in prompt.lower()
        assert "format" in prompt.lower()

    def test_verification_ok_regardless_of_wording(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "regardless" in prompt.lower()


class TestM106WorkerRobustCommands:
    """M106d: worker prompt includes robust command rules."""

    def test_no_find_root_rule(self):
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "find /" in prompt

    def test_command_v_recommended(self):
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "command -v" in prompt


class TestM48WorkerNoSudo:
    """48f: worker must not add sudo unless explicitly mentioned."""

    def test_48f_no_sudo_rule_present(self):
        """48f: worker prompt must have a rule about sudo usage."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "sudo" in prompt.lower()

    def test_48f_sudo_requires_explicit_mention(self):
        """48f: sudo must require explicit mention in task detail or system environment."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "explicit" in prompt.lower() or "explicitly" in prompt.lower()

    def test_48f_no_sudo_rule_says_do_not_add(self):
        """48f: rule must say not to add sudo unprompted."""
        prompt = (_ROLES_DIR / "worker.md").read_text()
        assert "not add" in prompt.lower() or "do not add" in prompt.lower() or "never add" in prompt.lower()


# --- M89c: _MAX_MESSENGER_FACTS constant ---


class TestMessengerFactsConstant:
    def test_max_messenger_facts_value(self):
        """M89c: _MAX_MESSENGER_FACTS must equal 50 (messenger context cap)."""
        from kiso.brain import _MAX_MESSENGER_FACTS
        assert _MAX_MESSENGER_FACTS == 50


# --- M92b: _group_facts_by_category ---


class TestGroupFactsByCategory:
    """Unit tests for the extracted _group_facts_by_category helper (M92b)."""

    def _fact(self, content, category="general", session=None):
        return {"content": content, "category": category, "session": session}

    def test_empty_returns_empty_list(self):
        from kiso.brain import _group_facts_by_category
        assert _group_facts_by_category([]) == []

    def test_all_four_categories_grouped(self):
        from kiso.brain import _group_facts_by_category
        facts = [
            self._fact("proj note", "project"),
            self._fact("user pref", "user"),
            self._fact("tool info", "tool"),
            self._fact("general note", "general"),
        ]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 4
        assert any("Project" in p for p in parts)
        assert any("User" in p for p in parts)
        assert any("Tool" in p for p in parts)
        assert any("General" in p for p in parts)

    def test_unknown_category_falls_to_general(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("unknown cat fact", category="obscure")]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 1
        assert "General" in parts[0]
        assert "unknown cat fact" in parts[0]

    def test_label_session_appends_session_tag(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("fact with session", "project", session="sess-abc")]
        parts = _group_facts_by_category(facts, label_session=True)
        assert "[session:sess-abc]" in parts[0]

    def test_label_session_false_no_tag(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("fact", "project", session="sess-abc")]
        parts = _group_facts_by_category(facts, label_session=False)
        assert "[session:" not in parts[0]

    def test_facts_without_session_not_labelled(self):
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("global fact", "project")]
        parts = _group_facts_by_category(facts, label_session=True)
        assert "[session:" not in parts[0]

    def test_empty_categories_absent_from_output(self):
        """Categories with no facts produce no section in the output."""
        from kiso.brain import _group_facts_by_category
        facts = [self._fact("only proj", "project")]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 1
        assert "Project" in parts[0]

    def test_fact_order_within_category_preserved(self):
        """Facts within a category appear in insertion order."""
        from kiso.brain import _group_facts_by_category
        facts = [
            self._fact("first", "user"),
            self._fact("second", "user"),
            self._fact("third", "user"),
        ]
        parts = _group_facts_by_category(facts)
        assert len(parts) == 1
        text = parts[0]
        assert text.index("first") < text.index("second") < text.index("third")

    def test_long_fact_truncated_at_200_chars(self):
        """Facts longer than _FACT_CHAR_LIMIT are truncated with ellipsis."""
        from kiso.brain import _group_facts_by_category, _FACT_CHAR_LIMIT
        long_content = "x" * (_FACT_CHAR_LIMIT + 50)
        facts = [self._fact(long_content, "project")]
        parts = _group_facts_by_category(facts)
        text = parts[0]
        # Should contain the truncated version, not the full string
        assert long_content not in text
        assert "x" * _FACT_CHAR_LIMIT in text
        assert "…" in text

    def test_short_fact_not_truncated(self):
        """Facts within _FACT_CHAR_LIMIT are kept intact."""
        from kiso.brain import _group_facts_by_category, _FACT_CHAR_LIMIT
        short_content = "y" * _FACT_CHAR_LIMIT
        facts = [self._fact(short_content, "project")]
        parts = _group_facts_by_category(facts)
        text = parts[0]
        assert short_content in text
        assert "…" not in text

    def test_fact_exactly_at_limit_not_truncated(self):
        """Facts exactly at _FACT_CHAR_LIMIT are not truncated."""
        from kiso.brain import _group_facts_by_category, _FACT_CHAR_LIMIT
        exact_content = "z" * _FACT_CHAR_LIMIT
        facts = [self._fact(exact_content, "project")]
        parts = _group_facts_by_category(facts)
        text = parts[0]
        assert exact_content in text
        assert "…" not in text


# --- M105a: _is_plugin_discovery_search ---


class TestIsPluginDiscoverySearch:
    """Unit tests for _is_plugin_discovery_search helper."""

    @pytest.mark.parametrize("detail", [
        "find browser skill in kiso registry",
        "cercare skill nel registro kiso",
        "search connector install",
        "kiso plugin discovery",
        "skill registry browse",
        "discover connector in registry",
        "search for available plugins in the registry",
        "find skill to install from kiso",
    ])
    def test_positive_matches(self, detail):
        assert _is_plugin_discovery_search(detail) is True

    @pytest.mark.parametrize("detail", [
        "latest python release",
        "browser automation tutorial",
        "how to install docker",
        "skill development best practices",
        "what is the weather today",
    ])
    def test_negative_matches(self, detail):
        assert _is_plugin_discovery_search(detail) is False


# --- M105a: validate_plan search-for-plugins ---


class TestValidatePlanPluginDiscovery:
    """M105a: search tasks for plugin discovery must be rejected."""

    def test_search_plugin_discovery_rejected(self):
        plan = {"tasks": [
            {"type": "search", "detail": "find browser skill in kiso registry",
             "expect": "skill info", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search cannot be used for kiso plugin discovery" in e for e in errors)

    def test_search_general_web_accepted(self):
        plan = {"tasks": [
            {"type": "search", "detail": "latest python release",
             "expect": "version info", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert not any("plugin discovery" in e for e in errors)

    def test_search_plugin_install_rejected(self):
        plan = {"tasks": [
            {"type": "search", "detail": "cercare skill browser nel registro",
             "expect": "info", "skill": None, "args": None},
            {"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None},
        ]}
        errors = validate_plan(plan)
        assert any("search cannot be used for kiso plugin discovery" in e for e in errors)


# --- M105b: exec translator passes max_tokens ---


class TestExecTranslatorMaxTokens:
    """M105b/M296: worker role gets max_tokens from MAX_TOKENS_DEFAULTS."""

    @pytest.mark.asyncio
    async def test_exec_translator_uses_default_max_tokens(self):
        config = _make_brain_config()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="echo hi") as mock_llm:
            await run_exec_translator(config, "print hi", "Linux x86_64", session="s1")
            mock_llm.assert_called_once()
            _, kwargs = mock_llm.call_args
            # M296: max_tokens no longer hardcoded — applied by call_llm from defaults
            assert "max_tokens" not in kwargs or kwargs.get("max_tokens") is None


# --- M105c: _repair_json ---


class TestRepairJson:
    """M105c: JSON repair — trailing commas + fences."""

    def test_trailing_comma_object(self):
        assert _repair_json('{"a": 1,}') == '{"a": 1}'

    def test_trailing_comma_array(self):
        assert _repair_json('[1, 2,]') == '[1, 2]'

    def test_nested_trailing_commas(self):
        repaired = _repair_json('{"a": [1,], "b": 2,}')
        parsed = json.loads(repaired)
        assert parsed == {"a": [1], "b": 2}

    def test_fences_and_comma(self):
        raw = '```json\n{"a": 1,}\n```'
        assert _repair_json(raw) == '{"a": 1}'

    def test_clean_passthrough(self):
        clean = '{"a": 1, "b": [2, 3]}'
        assert _repair_json(clean) == clean

    def test_whitespace_before_bracket(self):
        repaired = _repair_json('{"a": 1 ,  }')
        parsed = json.loads(repaired)
        assert parsed == {"a": 1}


# --- M105c: retry JSON error includes position ---


class TestRetryJsonErrorPosition:
    """M105c: retry feedback includes line/col info from JSONDecodeError."""

    @pytest.mark.asyncio
    async def test_retry_json_error_includes_position(self):
        config = _make_brain_config()
        valid_plan = json.dumps({
            "goal": "test", "secrets": None, "extend_replan": None,
            "tasks": [{"type": "msg", "detail": "done", "expect": None, "skill": None, "args": None}],
        })
        mock_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ]
        with patch("kiso.brain.build_planner_messages", new_callable=AsyncMock,
                    return_value=(mock_messages, [], [])):
            with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                        side_effect=["{invalid json!!!", valid_plan]) as mock_llm:
                plan = await run_planner(
                    db=AsyncMock(), config=config, session="s1",
                    user_role="admin", new_message="test",
                )
                assert plan["goal"] == "test"
                # Check that the retry feedback message contains "line" and "col"
                retry_msg = mock_llm.call_args_list[1][0][2]  # messages arg of second call
                feedback = retry_msg[-1]["content"]  # last message = error feedback
                assert "line" in feedback.lower()
                assert "col" in feedback.lower()


# --- M105d: messenger recent messages (chat context) ---


class TestBuildMessengerMessagesRecent:
    """M105d: build_messenger_messages includes recent messages when provided."""

    def test_recent_messages_included_in_context(self):
        config = _make_brain_config()
        recent = [
            {"role": "user", "user": "alice", "content": "Is browser installed?"},
            {"role": "assistant", "content": "Yes, browser skill is installed."},
        ]
        msgs = build_messenger_messages(
            config, "", [], "follow up question",
            recent_messages=recent,
        )
        user_content = msgs[1]["content"]
        assert "Recent Conversation" in user_content
        assert "Is browser installed?" in user_content
        assert "browser skill is installed" in user_content

    def test_no_recent_messages_no_section(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(config, "", [], "say hi")
        user_content = msgs[1]["content"]
        assert "Recent Conversation" not in user_content

    def test_recent_messages_none_no_section(self):
        config = _make_brain_config()
        msgs = build_messenger_messages(
            config, "", [], "say hi", recent_messages=None,
        )
        user_content = msgs[1]["content"]
        assert "Recent Conversation" not in user_content

    def test_recent_messages_fenced(self):
        """Recent messages are wrapped in security fences."""
        config = _make_brain_config()
        recent = [{"role": "user", "user": "bob", "content": "hello"}]
        msgs = build_messenger_messages(
            config, "", [], "reply", recent_messages=recent,
        )
        user_content = msgs[1]["content"]
        assert "<<<MESSAGES_" in user_content
        assert "<<<END_MESSAGES_" in user_content


class TestRunMessengerIncludeRecent:
    """M105d: run_messenger loads recent messages when include_recent=True."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        await save_message(conn, "sess1", None, "user", "Is browser installed?")
        await save_message(conn, "sess1", None, "assistant", "Yes it is.")
        yield conn
        await conn.close()

    @pytest.mark.asyncio
    async def test_include_recent_true_loads_messages(self, db):
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(
                db, config, "sess1", "I don't understand",
                include_recent=True,
            )
        user_content = captured_messages[1]["content"]
        assert "Recent Conversation" in user_content
        assert "Is browser installed?" in user_content

    @pytest.mark.asyncio
    async def test_include_recent_false_no_messages(self, db):
        config = _make_brain_config()
        captured_messages = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.extend(messages)
            return "ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_messenger(db, config, "sess1", "say hi")
        user_content = captured_messages[1]["content"]
        assert "Recent Conversation" not in user_content


# --- M146: Reviewer summary field ---


class TestM146ReviewerSummary:
    """M146: reviewer prompt instructs summary extraction."""

    def test_reviewer_prompt_mentions_summary(self):
        from kiso.brain import _load_system_prompt, invalidate_prompt_cache
        invalidate_prompt_cache()
        prompt = _load_system_prompt("reviewer")
        assert "summary" in prompt.lower()
        assert "500 chars" in prompt or "max 500" in prompt


# --- M147: Planner act-on-reviewer-hints rule ---


class TestM147PlannerActOnHints:
    """M147: planner prompt has act-on-reviewer-fixes rule."""

    def test_planner_prompt_act_on_fixes(self):
        from kiso.brain import _load_system_prompt, invalidate_prompt_cache
        invalidate_prompt_cache()
        prompt = _load_system_prompt("planner")
        assert "reviewer fixes" in prompt.lower()
        assert "never re-investigate" in prompt.lower() or "re-verify" in prompt.lower()


# --- M148: Reviewer summary covers failures too ---


class TestM148ReviewerSummaryFailures:
    """M148: reviewer prompt instructs summary for both successes and failures."""

    def test_reviewer_prompt_summary_covers_failures(self):
        from kiso.brain import _load_system_prompt, invalidate_prompt_cache
        invalidate_prompt_cache()
        prompt = _load_system_prompt("reviewer")
        assert "failures" in prompt.lower() or "failure" in prompt.lower()
        # Must mention both success and failure in summary context
        assert "BOTH" in prompt or "both" in prompt


class TestM186EscalatingValidationError:
    """M186: repeated identical validation errors get escalated."""

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            models=_full_models(),
            settings=_full_settings(max_validation_retries="5"),
            users={},
            raw={},
        )

    @pytest.mark.asyncio
    async def test_escalation_after_2_identical_errors(self, config):
        """After 2 identical validation errors, the 3rd feedback includes IMPORTANT."""
        call_count = [0]
        captured_messages = []

        async def mock_call_llm(cfg, role, messages, **kwargs):
            call_count[0] += 1
            captured_messages.append(list(messages))
            # Always return a valid JSON that fails validation the same way
            return json.dumps({"tasks": [
                {"type": "skill", "detail": "do", "skill": "browser",
                 "args": None, "expect": "done"},
                {"type": "msg", "detail": "done", "expect": None,
                 "skill": None, "args": None},
            ]})

        def always_fail(plan):
            return ["tool args invalid: missing required arg: action"]

        with patch("kiso.brain.call_llm", side_effect=mock_call_llm):
            with pytest.raises(PlanError):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, always_fail, PlanError, "Plan",
                )

        # Check that escalation happened on 3rd+ attempt
        # captured_messages[2] should have feedback with IMPORTANT
        assert call_count[0] == 5  # max retries
        # The 3rd call's messages should include IMPORTANT
        third_call_msgs = captured_messages[2]
        feedback = third_call_msgs[-1]["content"]
        assert "IMPORTANT" in feedback
        assert "same error" in feedback

    @pytest.mark.asyncio
    async def test_no_escalation_for_different_errors(self, config):
        """Different errors each time should not trigger escalation."""
        call_count = [0]
        captured_messages = []

        async def mock_call_llm(cfg, role, messages, **kwargs):
            call_count[0] += 1
            captured_messages.append(list(messages))
            return json.dumps({"value": call_count[0]})

        def varying_errors(plan):
            return [f"error number {call_count[0]}"]

        with patch("kiso.brain.call_llm", side_effect=mock_call_llm):
            with pytest.raises(PlanError):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, varying_errors, PlanError, "Plan",
                )

        # No feedback should contain IMPORTANT
        for msgs in captured_messages:
            for m in msgs:
                if m["role"] == "user" and "errors" in m.get("content", ""):
                    assert "IMPORTANT" not in m["content"]


class TestM194ReviewerDomainCheck:
    """M194: Reviewer prompt contains search domain cross-check rule."""

    def test_reviewer_prompt_has_domain_check_rule(self):
        """reviewer.md includes the search domain check rule."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "wrong domain" in prompt

    def test_reviewer_prompt_domain_check_mentions_replan(self):
        """Domain mismatch should trigger replan status."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "replan" in prompt
        assert "different domain" in prompt or "wrong domain" in prompt

    def test_build_reviewer_messages_contains_domain_rule(self):
        """build_reviewer_messages output includes the domain check rule."""
        msgs = build_reviewer_messages(
            goal="visit guidance.studio",
            detail="search for https://guidance.studio",
            expect="info about the site",
            output="guidestudio.com is a design firm...",
            user_message="go to guidance.studio",
        )
        system_content = msgs[0]["content"]
        assert "wrong domain" in system_content

    def test_m280_truncated_output_rule(self):
        """M280: reviewer prompt handles truncated output gracefully."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "[truncated]" in prompt
        assert "Do NOT replan just because truncated" in prompt or "Don't replan just because truncated" in prompt

    def test_m280_partial_success_rule(self):
        """M280: reviewer prompt defines partial success boundaries."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "Partial success" in prompt
        assert "warnings" in prompt.lower()


class TestM283SearcherPrompt:
    """M283: searcher prompt quality and language rules."""

    def test_searcher_prompt_exists(self):
        prompt = (_ROLES_DIR / "searcher.md").read_text()
        assert len(prompt) > 0

    def test_searcher_lang_matching(self):
        prompt = (_ROLES_DIR / "searcher.md").read_text()
        assert "query language controls output language" in prompt

    def test_searcher_source_quality(self):
        prompt = (_ROLES_DIR / "searcher.md").read_text()
        assert "primary sources" in prompt.lower()
        assert "official documentation" in prompt.lower()

    def test_searcher_domain_focus(self):
        prompt = (_ROLES_DIR / "searcher.md").read_text()
        assert "specific URL or domain" in prompt


class TestM418NoSilentAutoCorrect:
    """M418: Uninstalled skill plans raise PlanError (no silent auto-correction)."""

    UNINSTALLED_SKILL_PLAN = json.dumps({
        "goal": "Navigate to example.com",
        "secrets": None,
        "tasks": [
            {"type": "skill", "detail": "visit site", "skill": "browser",
             "args": '{"action": "navigate"}', "expect": "page loads"},
            {"type": "msg", "detail": "done", "skill": None, "args": None, "expect": None},
        ],
    })

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3, context_messages=5),
            raw={},
        )

    async def test_uninstalled_skill_raises_plan_error(self, db, config):
        """When planner always uses uninstalled skill, PlanError is raised (not auto-corrected)."""
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=self.UNINSTALLED_SKILL_PLAN):
            with pytest.raises(PlanError, match="validation failed"):
                await run_planner(db, config, "sess1", "admin", "visit example.com")

    async def test_non_skill_error_still_raises(self, db, config):
        """Non-skill validation errors still raise PlanError."""
        bad_plan = json.dumps({
            "goal": "test",
            "secrets": None,
            "tasks": [{"type": "exec", "detail": "ls", "skill": None,
                        "args": None, "expect": None}],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value=bad_plan):
            with pytest.raises(PlanError, match="validation failed"):
                await run_planner(db, config, "sess1", "admin", "do something")

    async def test_auto_correct_function_removed(self, db, config):
        """_auto_correct_uninstalled_skills no longer exists in brain module."""
        import kiso.brain
        assert not hasattr(kiso.brain, "_auto_correct_uninstalled_skills")


# ---------------------------------------------------------------------------
# Briefer tests (M242)
# ---------------------------------------------------------------------------


class TestBrieferMessages:
    """Tests for build_briefer_messages."""

    def test_minimal_context(self):
        msgs = build_briefer_messages("planner", "what time is it", {})
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert "briefer" in msgs[0]["content"].lower() or "context" in msgs[0]["content"].lower()
        assert "planner" in msgs[1]["content"]
        assert "what time is it" in msgs[1]["content"]
        assert "Available Modules" in msgs[1]["content"]

    def test_full_context_pool(self):
        pool = {
            "summary": "User asked about weather",
            "facts": "- Python 3.12 is installed",
            "recent_messages": "[user] marco: ciao",
            "tools": "browser: navigate, screenshot",
            "connectors": "telegram: messaging",
            "pending": "- What is your API key?",
            "paraphrased": "External user said hello",
            "replan_context": "Previous plan failed due to missing skill",
            "plan_outputs": "[0] exec: install browser\nStatus: done",
            "system_env": "OS: linux\nBinaries: python3, node",
            "capability_gap": "Skill 'browser' is needed but not installed.",
        }
        # Use "planner" — includes all sections (M272 skips some for messenger/worker)
        msgs = build_briefer_messages("planner", "plan task", pool)
        content = msgs[1]["content"]
        assert "Session Summary" in content
        assert "Known Facts" in content
        assert "Recent Messages" in content
        assert "Available Tools" in content
        assert "Available Connectors" in content
        assert "Pending Questions" in content
        assert "Paraphrased External Messages" in content
        assert "Replan Context" in content
        assert "Plan Outputs" in content
        assert "System Environment" in content
        assert "Capability Analysis" in content

    def test_empty_pool_values_excluded(self):
        pool = {"summary": "", "facts": "", "tools": "browser: navigate"}
        msgs = build_briefer_messages("planner", "do something", pool)
        content = msgs[1]["content"]
        assert "Session Summary" not in content
        assert "Known Facts" not in content
        assert "Available Tools" in content

    def test_consumer_role_in_message(self):
        for role in ("planner", "messenger", "worker"):
            msgs = build_briefer_messages(role, "task", {})
            assert role in msgs[1]["content"]

    def test_available_modules_listed(self):
        msgs = build_briefer_messages("planner", "task", {})
        content = msgs[1]["content"]
        for module in BRIEFER_MODULES:
            assert module in content

    def test_module_descriptions_included(self):
        """M259: briefer receives module descriptions, not just names."""
        msgs = build_briefer_messages("planner", "task", {})
        content = msgs[1]["content"]
        # Each module line has "- name: description" format
        assert "- planning_rules: task ordering" in content
        assert "- web: URLs, websites" in content
        assert "- replan: replan strategy" in content
        assert "- plugin_install: plugin discovery" in content

    def test_m426_module_descriptions_concise(self):
        """M426: each module description is ≤60 chars."""
        from kiso.brain import _BRIEFER_MODULE_DESCRIPTIONS
        for name, desc in _BRIEFER_MODULE_DESCRIPTIONS.items():
            assert len(desc) <= 60, f"{name}: '{desc}' is {len(desc)} chars (max 60)"

    def test_briefer_prompt_zero_module_guidance(self):
        """M259: briefer system prompt includes zero-module guidance."""
        msgs = build_briefer_messages("planner", "task", {})
        system = msgs[0]["content"]
        # Should mention that simple requests need zero/few modules
        assert "ZERO" in system or "core rules are sufficient" in system or "0-2 modules" in system

    def test_briefer_prompt_sys_env_guidance(self):
        """M259: briefer prompt includes sys_env filtering guidance."""
        msgs = build_briefer_messages("planner", "task", {})
        system = msgs[0]["content"]
        assert "System Environment" in system

    def test_m281_fast_path_examples(self):
        """M281: briefer prompt has explicit fast-path examples."""
        prompt = (_ROLES_DIR / "briefer.md").read_text()
        assert "Fast-path" in prompt
        assert "greetings" in prompt.lower()
        assert "Needs modules" in prompt

    def test_m281_conflict_handling(self):
        """M281: briefer prompt has conflict handling guidance."""
        prompt = (_ROLES_DIR / "briefer.md").read_text()
        assert "Conflicting facts" in prompt
        assert "most recent" in prompt.lower()

    def test_m368_briefer_prompt_no_opinions(self):
        """M368: briefer prompt prohibits opinions/interpretations in context."""
        prompt = (_ROLES_DIR / "briefer.md").read_text()
        assert "NEVER add opinions" in prompt

    def test_m368_briefer_prompt_no_inferences(self):
        """M368: briefer prompt prohibits inferences not in input."""
        prompt = (_ROLES_DIR / "briefer.md").read_text()
        assert "inferences" in prompt.lower()
        assert "not present in the input" in prompt.lower()

    def test_m265_messenger_no_modules_or_skills_rule(self):
        """M265: briefer prompt says messenger gets modules=[] and tools=[] always."""
        msgs = build_briefer_messages("messenger", "tell the user what happened", {})
        system = msgs[0]["content"]
        assert "For messenger/worker: modules=[] and tools=[] always" in system

    def test_m265_worker_no_modules_or_tools_rule(self):
        """M265: briefer prompt says worker gets modules=[] and tools=[] always."""
        msgs = build_briefer_messages("worker", "translate command", {})
        system = msgs[0]["content"]
        assert "For messenger/worker: modules=[] and tools=[] always" in system


class TestValidateBriefing:
    """Tests for validate_briefing."""

    def test_valid_briefing(self):
        briefing = {
            "modules": ["web"],
            "tools": ["browser: navigate, screenshot"],
            "context": "User wants to visit a website",
            "output_indices": [0, 2],
            "relevant_tags": ["browser"],
            "relevant_entities": [],
        }
        assert validate_briefing(briefing) == []

    def test_empty_briefing(self):
        briefing = {
            "modules": [],
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        assert validate_briefing(briefing) == []

    def test_unknown_module(self):
        briefing = {
            "modules": ["web", "nonexistent_module"],
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        errors = validate_briefing(briefing)
        assert len(errors) == 1
        assert "nonexistent_module" in errors[0]

    def test_invalid_modules_type(self):
        briefing = {
            "modules": "web",
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        errors = validate_briefing(briefing)
        assert any("modules" in e for e in errors)

    def test_invalid_context_type(self):
        briefing = {
            "modules": [],
            "tools": [],
            "context": None,
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        errors = validate_briefing(briefing)
        assert any("context" in e for e in errors)

    def test_all_valid_modules(self):
        briefing = {
            "modules": list(BRIEFER_MODULES),
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        assert validate_briefing(briefing) == []

    def test_invalid_relevant_tags_type(self):
        """M250: relevant_tags must be an array."""
        briefing = {
            "modules": [],
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": "browser",  # should be array
        }
        errors = validate_briefing(briefing)
        assert any("relevant_tags" in e for e in errors)

    def test_missing_relevant_tags(self):
        """M250: missing relevant_tags is an error."""
        briefing = {
            "modules": [],
            "tools": [],
            "context": "",
            "output_indices": [],
        }
        errors = validate_briefing(briefing)
        assert any("relevant_tags" in e for e in errors)


class TestRunBriefer:
    """Tests for run_briefer."""

    @pytest.fixture
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"deepseek": Provider(base_url="http://localhost")},
            users={},
            models=_full_models(),
            settings=_full_settings(),
            raw={},
        )

    @pytest.mark.asyncio
    async def test_success(self, config):
        response = json.dumps({
            "modules": ["web"],
            "tools": ["browser: navigate"],
            "context": "User wants to browse",
            "output_indices": [1],
            "relevant_tags": ["browser"],
            "relevant_entities": [],
        })
        ctx = {"tools": "Available skills:\n- browser — Navigate, click, fill"}
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "planner", "visit a website", ctx)
        assert result["modules"] == ["web"]
        assert result["tools"] == ["browser: navigate"]
        assert result["context"] == "User wants to browse"
        assert result["output_indices"] == [1]
        assert result["relevant_tags"] == ["browser"]

    @pytest.mark.asyncio
    async def test_empty_briefing(self, config):
        response = json.dumps({
            "modules": [],
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "planner", "what time is it", {})
        assert result["modules"] == []
        assert result["tools"] == []

    @pytest.mark.asyncio
    async def test_llm_error_raises_briefer_error(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("connection failed")):
            with pytest.raises(BrieferError):
                await run_briefer(config, "planner", "task", {})

    @pytest.mark.asyncio
    async def test_invalid_json_retries_then_fails(self, config):
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="not json at all"):
            with pytest.raises(BrieferError):
                await run_briefer(config, "planner", "task", {})

    @pytest.mark.asyncio
    async def test_m368_filters_hallucinated_skills(self, config):
        """M368: run_briefer filters skills not matching context pool."""
        response = json.dumps({
            "modules": [],
            "tools": ["browser: navigate and screenshot", "Retrieve CPU details"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        ctx = {"tools": "Available skills:\n- browser — navigate, click, fill, screenshot, text"}
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "planner", "visit example.com", ctx)
        # "browser" matches context pool, "Retrieve CPU details" does not
        assert any("browser" in s for s in result["tools"])
        assert not any("Retrieve" in s for s in result["tools"])

    @pytest.mark.asyncio
    async def test_m368_preserves_valid_skills(self, config):
        """M368: run_briefer preserves skills that match context pool."""
        response = json.dumps({
            "modules": [],
            "tools": ["search: web search for queries"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        ctx = {"tools": "Available skills:\n- search — web search for queries, max_results option"}
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "planner", "find info", ctx)
        assert len(result["tools"]) == 1
        assert "search" in result["tools"][0]

    @pytest.mark.asyncio
    async def test_m387_clears_skills_when_none_installed(self, config):
        """M387: all briefer skills cleared when no skills in context pool."""
        response = json.dumps({
            "modules": [],
            "tools": ["browser: navigate", "aider: code refactoring"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "planner", "task", {})
        # No skills installed → all hallucinated skills cleared
        assert result["tools"] == []

    @pytest.mark.asyncio
    async def test_m387_clears_skills_with_empty_string_pool(self, config):
        """M387: all briefer skills cleared when skills key is empty string."""
        response = json.dumps({
            "modules": [],
            "tools": ["browser: navigate"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "planner", "task", {"tools": ""})
        assert result["tools"] == []

    @pytest.mark.asyncio
    async def test_m387_no_skills_returned_passes_through(self, config):
        """M387: when briefer returns no skills, nothing to filter."""
        response = json.dumps({
            "modules": [],
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "planner", "task", {})
        assert result["tools"] == []


class TestBrieferSchema:
    """Tests for BRIEFER_SCHEMA validity."""

    def test_schema_validates_valid_briefing(self):
        valid = {
            "modules": ["web", "replan"],
            "tools": ["browser: navigate"],
            "context": "some context",
            "output_indices": [0, 1, 2],
            "relevant_tags": ["browser", "tech-stack"],
            "relevant_entities": [],
        }
        _jsonschema.validate(valid, BRIEFER_SCHEMA["json_schema"]["schema"])

    def test_schema_rejects_missing_field(self):
        invalid = {
            "modules": ["web"],
            "tools": [],
            "context": "",
            # missing output_indices and relevant_tags
        }
        with pytest.raises(_jsonschema.ValidationError):
            _jsonschema.validate(invalid, BRIEFER_SCHEMA["json_schema"]["schema"])

    def test_schema_rejects_wrong_type(self):
        invalid = {
            "modules": "web",  # should be array
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        with pytest.raises(_jsonschema.ValidationError):
            _jsonschema.validate(invalid, BRIEFER_SCHEMA["json_schema"]["schema"])

    def test_schema_validates_empty_relevant_tags(self):
        """M250: empty relevant_tags is valid."""
        valid = {
            "modules": [],
            "tools": [],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        _jsonschema.validate(valid, BRIEFER_SCHEMA["json_schema"]["schema"])


# ---------------------------------------------------------------------------
# _load_modular_prompt (M243)
# ---------------------------------------------------------------------------


class TestLoadModularPrompt:
    """Tests for _load_modular_prompt — module marker parsing."""

    def test_planner_core_only(self):
        """Loading only core returns identity + task types without any other modules."""
        result = _load_modular_prompt("planner", [])
        assert "Kiso planner" in result
        assert "Task types:" in result
        assert "Last task must" in result
        # All conditional modules should be absent
        assert "Web interaction:" not in result
        assert "Scripting:" not in result
        assert "extend_replan" not in result
        assert "Broken tool recovery" not in result
        assert "File-based data flow" not in result
        assert "Tools efficiency:" not in result
        assert "Kiso-native first" not in result
        assert "Recent Messages" not in result

    def test_planner_core_plus_web(self):
        """Loading core + web includes web rules but not others."""
        result = _load_modular_prompt("planner", ["web"])
        assert "Kiso planner" in result
        assert "Web interaction:" in result
        assert "browser" in result.lower()
        # Other modules absent
        assert "Scripting:" not in result
        assert "extend_replan" not in result

    def test_planner_core_plus_replan(self):
        """Loading core + replan includes replan strategy rules."""
        result = _load_modular_prompt("planner", ["replan"])
        assert "extend_replan" in result
        assert "Strategy diversification" in result or "fundamentally different strategy" in result
        assert "reviewer fixes" in result
        # Others absent
        assert "Web interaction:" not in result
        assert "One-liner execution" not in result and "One-liners" not in result

    def test_planner_core_plus_scripting(self):
        """Loading core + scripting includes scripting rules."""
        result = _load_modular_prompt("planner", ["scripting"])
        assert "One-liner execution" in result or "One-liners" in result
        assert "python -c" in result
        assert "Web interaction:" not in result

    def test_planner_core_plus_tool_recovery(self):
        """Loading core + tool_recovery includes broken tool rules."""
        result = _load_modular_prompt("planner", ["tool_recovery"])
        assert "Broken tool deps" in result
        assert "kiso tool remove" in result

    def test_planner_core_plus_data_flow(self):
        """Loading core + data_flow includes file-based data flow rules."""
        result = _load_modular_prompt("planner", ["data_flow"])
        assert "save to file" in result
        assert "truncated at 4KB" in result

    def test_planner_core_plus_planning_rules(self):
        """Loading core + planning_rules includes general planning rules."""
        result = _load_modular_prompt("planner", ["planning_rules"])
        assert "Kiso planner" in result
        assert "Recent Messages" in result
        assert "non-null" in result and "`expect`" in result
        assert "fabricate" in result
        # Other modules absent
        assert "Tools efficiency:" not in result
        assert "Kiso-native first" not in result

    def test_planner_core_plus_kiso_native(self):
        """Loading core + kiso_native includes kiso-first policy."""
        result = _load_modular_prompt("planner", ["kiso_native"])
        assert "Kiso-native first" in result
        assert "kiso env set" in result.lower()
        # Other modules absent
        assert "Tools efficiency:" not in result
        assert "Recent Messages" not in result

    def test_planner_core_plus_tools_rules(self):
        """Loading core + tools_rules includes tool efficiency rules."""
        result = _load_modular_prompt("planner", ["tools_rules"])
        assert "Tools efficiency:" in result
        assert "atomic" in result
        assert "Task ordering" in result
        # Other modules absent
        assert "Kiso-native first" not in result
        assert "Recent Messages" not in result

    def test_planner_core_plus_kiso_commands(self):
        """Loading core + kiso_commands includes CLI commands."""
        result = _load_modular_prompt("planner", ["kiso_commands"])
        assert "kiso tool install" in result
        assert "kiso connector install" in result
        assert "kiso env set" in result

    def test_planner_core_plus_user_mgmt(self):
        """Loading core + user_mgmt includes user management rules."""
        result = _load_modular_prompt("planner", ["user_mgmt"])
        assert "PROTECTION" in result or "Caller Role" in result
        assert "kiso user add" in result

    def test_planner_core_plus_plugin_install(self):
        """Loading core + plugin_install includes plugin installation procedure."""
        result = _load_modular_prompt("planner", ["plugin_install"])
        assert "Plugin installation:" in result
        assert "raw.githubusercontent.com" in result
        assert "kiso.toml" in result

    def test_all_modules_returns_full_content(self):
        """Loading all modules returns content equivalent to the full prompt."""
        modular = _load_modular_prompt("planner", list(BRIEFER_MODULES))
        # All key sections present
        assert "Kiso planner" in modular
        assert "Web interaction:" in modular
        assert "One-liner execution" in modular or "One-liners" in modular
        assert "extend_replan" in modular
        assert "Broken tool deps" in modular
        assert "save to file" in modular
        assert "Tools efficiency:" in modular
        assert "Kiso-native first" in modular
        assert "Recent Messages" in modular
        # Former appendixes now modules
        assert "kiso tool install" in modular
        assert "PROTECTION" in modular or "Caller Role" in modular
        assert "Plugin installation:" in modular

    def test_no_markers_returns_full_prompt(self):
        """Prompt without markers returns the full text (backward compat)."""
        prompt_text = "You are a test role.\nNo markers here."
        with patch("kiso.brain._load_system_prompt", return_value=prompt_text):
            result = _load_modular_prompt("testrole", ["web"])
        assert result == prompt_text

    def test_multiple_modules_combined(self):
        """Loading multiple modules concatenates them with core."""
        result = _load_modular_prompt("planner", ["web", "scripting", "data_flow"])
        assert "Web interaction:" in result
        assert "One-liner execution" in result or "One-liners" in result
        assert "save to file" in result
        assert "extend_replan" not in result
        assert "Broken tool deps" not in result


# ---------------------------------------------------------------------------
# Briefer integration for planner (M244)
# ---------------------------------------------------------------------------


class TestBrieferPlannerIntegration:
    """Tests for briefer integration in build_planner_messages."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _config(self, briefer_enabled=True):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(
                context_messages=3,
                briefer_enabled=briefer_enabled,
            ),
            raw={},
        )

    async def test_briefer_selects_modules(self, db):
        """When briefer succeeds, planner prompt uses selected modules only."""
        briefing = {
            "modules": ["web"],
            "tools": ["browser: navigate, screenshot"],
            "context": "User wants to browse a website.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return json.dumps({
                "goal": "browse", "secrets": None,
                "tasks": [{"type": "msg", "detail": "Answer in English. done",
                           "skill": None, "args": None, "expect": None}],
            })

        # M387: provide browser skill so briefer skill selection isn't cleared
        fake_skills = [
            {"name": "browser", "summary": "Navigate, click, fill, screenshot, text",
             "args_schema": {}, "env": {}, "session_secrets": [],
             "path": "/fake", "version": "0.1.0", "description": ""},
        ]
        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "admin", "go to example.com",
            )

        system = msgs[0]["content"]
        user_content = msgs[1]["content"]
        # Web module included
        assert "Web interaction:" in system
        # Replan module excluded
        assert "extend_replan" not in system
        # Briefer's synthesized context used
        assert "## Context\nUser wants to browse a website." in user_content
        # Briefer-filtered skills used
        assert "browser: navigate, screenshot" in user_content
        # M258: sys_env NOT unconditionally included in briefer path
        assert "## System Environment" not in user_content

    async def test_briefer_disabled_uses_full_context(self, db):
        """When briefer_enabled=False, full context is used (original behavior)."""
        config = self._config(briefer_enabled=False)
        with patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "admin", "hello",
            )

        system = msgs[0]["content"]
        # Full prompt with all modules
        assert "Kiso planner" in system
        # User content has standard sections
        user_content = msgs[1]["content"]
        assert "## System Environment" in user_content
        assert "## New Message" in user_content

    async def test_briefer_failure_falls_back(self, db):
        """When briefer raises, falls back to full context gracefully."""
        config = self._config(briefer_enabled=True)

        async def _failing_llm(cfg, role, messages, **kw):
            if role == "briefer":
                raise LLMError("briefer down")
            return json.dumps({
                "goal": "test", "secrets": None,
                "tasks": [{"type": "msg", "detail": "Answer in English. hi",
                           "skill": None, "args": None, "expect": None}],
            })

        with patch("kiso.brain.call_llm", side_effect=_failing_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "what time is it?",
            )

        system = msgs[0]["content"]
        # Full prompt (fallback)
        assert "Kiso planner" in system
        user_content = msgs[1]["content"]
        # Standard sections present (fallback path)
        assert "## System Environment" in user_content
        assert "## New Message" in user_content

    async def test_briefer_context_replaces_raw_sections(self, db):
        """With briefer, raw summary/facts/recent are replaced by synthesized context."""
        briefing = {
            "modules": [],
            "tools": [],
            "context": "Synthesized context from briefer.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "hello",
            )

        user_content = msgs[1]["content"]
        # Briefer's synthesized context present
        assert "Synthesized context from briefer." in user_content
        # Raw sections NOT present
        assert "## Session Summary" not in user_content
        assert "## Known Facts" not in user_content
        assert "## Recent Messages" not in user_content

    async def test_appendices_still_injected_with_briefer(self, db):
        """Keyword-based appendices are still injected even when briefer is active."""
        briefing = {
            "modules": [],
            "tools": [],
            "context": "User wants to install a skill.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "install the browser skill",
            )

        system = msgs[0]["content"]
        # Plugin-install appendix injected by keyword matching
        assert "plugin" in system.lower() or "install" in system.lower()


# ---------------------------------------------------------------------------
# Briefer tag-based fact retrieval (M250)
# ---------------------------------------------------------------------------


class TestBrieferTagRetrieval:
    """Tests for briefer using tags to retrieve additional facts."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _config(self, briefer_enabled=True):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(
                context_messages=3,
                briefer_enabled=briefer_enabled,
            ),
            raw={},
        )

    async def test_tag_matched_facts_appended(self, db):
        """M250: briefer's relevant_tags trigger tag-based fact retrieval."""
        # Save facts: one matched by FTS5, one only reachable by tag.
        # Use a unique keyword in the FTS fact so FTS5 returns it (not fallback).
        await save_fact(db, "Python version 3.12 deployed", "test", category="project")
        tag_only_id = await save_fact(db, "Redis cache on port 6379", "test", category="project")
        await save_fact_tags(db, tag_only_id, ["infra", "cache"])

        briefing = {
            "modules": [],
            "tools": [],
            "context": "User asks about infrastructure.",
            "output_indices": [],
            "relevant_tags": ["infra", "cache"],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "Python version",
            )

        user_content = msgs[1]["content"]
        # Tag-matched fact appears in additional section
        assert "Redis cache on port 6379" in user_content
        assert "## Relevant Facts" in user_content

    async def test_no_duplicate_facts(self, db):
        """M390: facts matching both tags and keywords appear exactly once."""
        # Save a fact that matches both keywords and tags
        fid = await save_fact(db, "Python version 3.12 deployed", "test", category="project")
        await save_fact_tags(db, fid, ["tech-stack"])

        briefing = {
            "modules": [],
            "tools": [],
            "context": "User asks about Python.",
            "output_indices": [],
            "relevant_tags": ["tech-stack"],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "Python version",
            )

        user_content = msgs[1]["content"]
        # Fact appears in unified Relevant Facts section, exactly once
        assert "## Relevant Facts" in user_content
        assert user_content.count("Python version 3.12 deployed") == 1

    async def test_empty_relevant_tags_no_section(self, db):
        """M250: empty relevant_tags produces no additional facts section."""
        briefing = {
            "modules": [],
            "tools": [],
            "context": "Simple question.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "hello",
            )

        user_content = msgs[1]["content"]
        assert "## Relevant Facts" not in user_content

    async def test_available_tags_in_briefer_context(self, db):
        """M250: available tags are passed to the briefer in the context pool."""
        # Save tagged facts so tags exist
        fid = await save_fact(db, "Uses PostgreSQL", "test", category="project")
        await save_fact_tags(db, fid, ["database", "postgres"])

        captured_messages = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps({
                    "modules": [], "tools": [], "context": "",
                    "output_indices": [], "relevant_tags": [],
                    "relevant_entities": [],
                })
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            await build_planner_messages(
                db, config, "sess1", "user", "tell me about the db",
            )

        # Briefer should receive available tags in its context
        briefer_user_content = captured_messages[1]["content"]
        assert "database" in briefer_user_content
        assert "postgres" in briefer_user_content
        assert "Available Fact Tags" in briefer_user_content

    async def test_fallback_no_tags_exist(self, db):
        """M250: when no tags exist, no available_tags section in briefer context."""
        captured_messages = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps({
                    "modules": [], "tools": [], "context": "",
                    "output_indices": [], "relevant_tags": [],
                    "relevant_entities": [],
                })
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            await build_planner_messages(
                db, config, "sess1", "user", "hello",
            )

        briefer_user_content = captured_messages[1]["content"]
        assert "Available Fact Tags" not in briefer_user_content


# ---------------------------------------------------------------------------
# M346 — Briefer entity-scoped retrieval
# ---------------------------------------------------------------------------


class TestM346BrieferEntityRetrieval:
    """M346: briefer uses relevant_entities for entity-scoped fact retrieval."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(context_messages=3, briefer_enabled=True),
            raw={},
        )

    async def test_entity_facts_appended(self, db):
        """M346: relevant_entities retrieves all entity-linked facts."""
        from kiso.store import find_or_create_entity
        eid = await find_or_create_entity(db, "acmecorp", "company")
        await save_fact(db, "acmecorp uses Webflow CMS", "curator", entity_id=eid)
        await save_fact(db, "acmecorp has contact form", "curator", entity_id=eid)
        # Add a distractor fact that matches the FTS query so FTS5 doesn't
        # fall back to get_facts() (which would return everything).
        await save_fact(db, "Python version 3.12 deployed", "test", category="project")

        briefing = {
            "modules": [], "tools": [], "context": "User asks about their company.",
            "output_indices": [], "relevant_tags": [],
            "relevant_entities": ["acmecorp"],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, self._config(), "sess1", "user", "Python version",
            )

        user_content = msgs[1]["content"]
        assert "acmecorp uses Webflow CMS" in user_content
        assert "acmecorp has contact form" in user_content
        assert "## Relevant Facts" in user_content

    async def test_entity_facts_dedup_against_keywords(self, db):
        """M390: entity facts matching keywords appear exactly once in scored results."""
        from kiso.store import find_or_create_entity
        eid = await find_or_create_entity(db, "flask", "tool")
        # This fact matches both entity and keywords
        await save_fact(db, "Flask web framework version 3.0", "curator", entity_id=eid)

        briefing = {
            "modules": [], "tools": [], "context": "About Flask.",
            "output_indices": [], "relevant_tags": [],
            "relevant_entities": ["flask"],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, self._config(), "sess1", "user", "Flask web framework",
            )

        user_content = msgs[1]["content"]
        # Fact appears once in unified Relevant Facts section
        assert "## Relevant Facts" in user_content
        assert user_content.count("Flask web framework version 3.0") == 1

    async def test_entities_in_briefer_context_pool(self, db):
        """M346: available entities appear in briefer context pool."""
        from kiso.store import find_or_create_entity
        await find_or_create_entity(db, "flask", "tool")

        captured_messages = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps({
                    "modules": [], "tools": [], "context": "",
                    "output_indices": [], "relevant_tags": [],
                    "relevant_entities": [],
                })
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            await build_planner_messages(
                db, self._config(), "sess1", "user", "hello",
            )

        briefer_content = captured_messages[1]["content"]
        assert "flask" in briefer_content
        assert "Available Entities" in briefer_content

    async def test_empty_relevant_entities_no_section(self, db):
        """M346: empty relevant_entities produces no entity-matched section."""
        briefing = {
            "modules": [], "tools": [], "context": "Simple.",
            "output_indices": [], "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, self._config(), "sess1", "user", "hello",
            )

        assert "## Relevant Facts" not in msgs[1]["content"]


# ---------------------------------------------------------------------------
# M258 — sys_env and capability_gap removed from planner in briefer path
# ---------------------------------------------------------------------------


class TestM258SysEnvAndGapFiltering:
    """M258: sys_env and capability_gap go through briefer, not unconditional."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _config(self, briefer_enabled=True):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(
                context_messages=3,
                briefer_enabled=briefer_enabled,
            ),
            raw={},
        )

    async def test_briefer_path_no_sys_env_in_user_content(self, db):
        """M258: briefer path does NOT unconditionally append sys_env."""
        briefing = {
            "modules": [],
            "tools": [],
            "context": "User wants a joke.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "tell me a joke",
            )

        user_content = msgs[1]["content"]
        assert "## System Environment" not in user_content
        assert "## Context\nUser wants a joke." in user_content

    async def test_fallback_path_has_sys_env(self, db):
        """M258: fallback path (no briefer) still includes sys_env."""
        config = self._config(briefer_enabled=False)
        with patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "admin", "hello",
            )

        user_content = msgs[1]["content"]
        assert "## System Environment" in user_content

    async def test_capability_gap_in_context_pool_for_briefer(self, db):
        """M258: capability_gap is passed to briefer in context_pool."""
        captured_messages = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps({
                    "modules": ["plugin_install"],
                    "tools": [],
                    "context": "User wants to take a screenshot. Browser skill missing.",
                    "output_indices": [],
                    "relevant_tags": [],
                    "relevant_entities": [],
                })
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "take a screenshot",
            )

        # Briefer should receive capability gap in its context
        briefer_content = captured_messages[1]["content"]
        assert "Capability Analysis" in briefer_content
        assert "browser" in briefer_content

        # Planner user content should NOT have raw capability gap
        user_content = msgs[1]["content"]
        assert "## Capability Analysis" not in user_content

    async def test_capability_gap_in_fallback_path(self, db):
        """M258: fallback path still unconditionally includes capability_gap."""
        config = self._config(briefer_enabled=False)
        with patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "take a screenshot",
            )

        user_content = msgs[1]["content"]
        assert "## Capability Analysis" in user_content
        assert "browser" in user_content

    async def test_sys_env_in_briefer_context_pool(self, db):
        """M258: sys_env is available to the briefer via context_pool."""
        captured_messages = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                captured_messages.extend(messages)
                return json.dumps({
                    "modules": [],
                    "tools": [],
                    "context": "Simple request.",
                    "output_indices": [],
                    "relevant_tags": [],
                    "relevant_entities": [],
                })
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            await build_planner_messages(
                db, config, "sess1", "user", "hello",
            )

        # Briefer should see system environment in its context
        briefer_content = captured_messages[1]["content"]
        assert "System Environment" in briefer_content


# ---------------------------------------------------------------------------
# M266 — Web module: warn when browser not installed
# ---------------------------------------------------------------------------


class TestM266BrowserAvailability:
    """M266: planner gets browser warning when web module active but browser not installed."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _config(self, briefer_enabled=True):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(
                context_messages=3,
                briefer_enabled=briefer_enabled,
            ),
            raw={},
        )

    async def test_web_module_no_browser_shows_warning(self, db):
        """Briefer selects web module, browser not installed → warning present."""
        briefing = {
            "modules": ["web"],
            "tools": [],
            "context": "User wants to visit guidance.studio.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "vai su guidance.studio",
            )

        user_content = msgs[1]["content"]
        assert "## Browser Availability" in user_content
        assert "browser tool is NOT currently installed" in user_content

    async def test_web_module_with_browser_installed_no_warning(self, db):
        """Briefer selects web module, browser IS installed → no warning."""
        briefing = {
            "modules": ["web"],
            "tools": ["browser — navigate pages"],
            "context": "User wants to visit guidance.studio.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        fake_skill = {
            "name": "browser", "summary": "browser automation",
            "args": [], "guide": "",
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[fake_skill]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "vai su guidance.studio",
            )

        user_content = msgs[1]["content"]
        assert "## Browser Availability" not in user_content

    async def test_no_web_module_no_warning(self, db):
        """Briefer does NOT select web module → no warning regardless."""
        briefing = {
            "modules": [],
            "tools": [],
            "context": "User wants a joke.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        config = self._config(briefer_enabled=True)
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "tell me a joke",
            )

        user_content = msgs[1]["content"]
        assert "## Browser Availability" not in user_content

    async def test_fallback_path_web_module_no_browser(self, db):
        """Fallback path (no briefer) also shows warning when web module active."""
        config = self._config(briefer_enabled=False)
        with patch("kiso.brain.discover_tools", return_value=[]):
            # "go to" triggers web module via fallback_modules (web is always included)
            msgs, _, _ = await build_planner_messages(
                db, config, "sess1", "user", "go to guidance.studio",
            )

        user_content = msgs[1]["content"]
        assert "## Browser Availability" in user_content


# ---------------------------------------------------------------------------
# M261 — End-to-end token reduction validation
# ---------------------------------------------------------------------------


class TestM261PromptSizeReduction:
    """M261: verify planner prompt size decreases with selective module loading."""

    def test_core_only_is_smallest(self):
        """Core-only prompt (no modules) is significantly smaller than all modules."""
        core_only = _load_modular_prompt("planner", [])
        all_modules = _load_modular_prompt("planner", list(BRIEFER_MODULES))
        # Core-only should be less than 30% of full prompt (+M353 self-identity rules)
        assert len(core_only) < len(all_modules) * 0.30

    def test_core_plus_web_is_small(self):
        """Core + web module is much smaller than full prompt."""
        core_web = _load_modular_prompt("planner", ["web"])
        all_modules = _load_modular_prompt("planner", list(BRIEFER_MODULES))
        assert len(core_web) < len(all_modules) * 0.40

    def test_install_scenario_moderate(self):
        """Install scenario includes only relevant modules, not all."""
        install_prompt = _load_modular_prompt(
            "planner", ["planning_rules", "kiso_native", "tools_rules", "plugin_install"],
        )
        all_modules = _load_modular_prompt("planner", list(BRIEFER_MODULES))
        # Install scenario should be roughly 50-70% of full
        assert len(install_prompt) < len(all_modules) * 0.75

    def test_replan_scenario_small(self):
        """Replan scenario (core + replan + tool_recovery) is compact."""
        replan_prompt = _load_modular_prompt("planner", ["replan", "tool_recovery"])
        all_modules = _load_modular_prompt("planner", list(BRIEFER_MODULES))
        assert len(replan_prompt) < len(all_modules) * 0.40

    def test_all_modules_cover_all_content(self):
        """All modules combined include all the content from planner.md."""
        all_modules = _load_modular_prompt("planner", list(BRIEFER_MODULES))
        # Key content from each module should be present
        assert "Kiso planner" in all_modules  # core
        assert "Kiso-native first" in all_modules  # kiso_native
        assert "natural language WHAT" in all_modules  # planning_rules
        assert "atomic" in all_modules  # tools_rules
        assert "apt-get" in all_modules  # tool_recovery
        assert "save to file" in all_modules  # data_flow
        assert "Web interaction" in all_modules  # web
        assert "One-liner" in all_modules  # scripting
        assert "extend_replan" in all_modules  # replan
        assert "kiso tool install" in all_modules  # kiso_commands
        assert "never generate" in all_modules  # user_mgmt
        assert "Plugin installation" in all_modules  # plugin_install


class TestM261BrieferModuleCoverage:
    """M261: verify briefer path covers what keyword matching used to handle."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(context_messages=3, briefer_enabled=True),
            raw={},
        )

    def _fake_skill(self):
        return [{"name": "dummy", "summary": "test skill", "args_schema": {}}]

    async def _run_with_briefer_modules(self, db, message, modules):
        """Run build_planner_messages with a briefer that returns given modules."""
        briefing = {
            "modules": modules,
            "tools": [],
            "context": "Briefer context.",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        # Provide a fake skill so plugin_install safety net doesn't trigger
        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.brain.discover_tools", return_value=self._fake_skill()):
            msgs, _, _ = await build_planner_messages(
                db, self._config(), "sess1", "user", message,
            )
        return msgs[0]["content"]  # system prompt

    async def test_plugin_install_module_selected(self, db):
        """Briefer selecting plugin_install covers old keyword matching."""
        system = await self._run_with_briefer_modules(
            db, "install the browser skill", ["plugin_install"],
        )
        assert "Plugin installation" in system

    async def test_kiso_commands_module_selected(self, db):
        """Briefer selecting kiso_commands covers old kiso keyword matching."""
        system = await self._run_with_briefer_modules(
            db, "list kiso envs", ["kiso_commands"],
        )
        assert "kiso tool install" in system

    async def test_user_mgmt_module_selected(self, db):
        """Briefer selecting user_mgmt covers old user keyword matching."""
        system = await self._run_with_briefer_modules(
            db, "add user marco", ["user_mgmt"],
        )
        assert "PROTECTION" in system or "Caller Role" in system

    async def test_zero_modules_for_simple_query(self, db):
        """Simple query with zero modules gets core-only prompt."""
        system = await self._run_with_briefer_modules(
            db, "what time is it?", [],
        )
        # Core content present
        assert "Kiso planner" in system
        # Module-specific content absent
        assert "extend_replan" not in system
        assert "Web interaction" not in system
        assert "Plugin installation" not in system


class TestM261MessengerContextReduction:
    """M261: verify messenger briefer filters plan_outputs effectively."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_messenger_receives_filtered_outputs(self, db):
        """M261: messenger with briefer receives only relevant outputs."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(context_messages=3, briefer_enabled=True),
            raw={},
        )
        # Simulate 5 plan outputs, briefer selects only index 4 and 5
        plan_outputs = [
            {"index": 1, "type": "exec", "detail": "install deps", "output": "ok", "status": "done"},
            {"index": 2, "type": "exec", "detail": "check env", "output": "ok", "status": "done"},
            {"index": 3, "type": "exec", "detail": "download data", "output": "ok", "status": "done"},
            {"index": 4, "type": "search", "detail": "weather rome", "output": "Sunny 25C", "status": "done"},
            {"index": 5, "type": "exec", "detail": "format results", "output": "Rome: Sunny", "status": "done"},
        ]

        captured_messages = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps({
                    "modules": [], "tools": [],
                    "context": "User asked about weather in Rome.",
                    "output_indices": [4, 5],
                    "relevant_tags": [],
                    "relevant_entities": [],
                })
            captured_messages.extend(messages)
            return "The weather in Rome is sunny."

        from kiso.worker.loop import _msg_task
        with patch("kiso.brain.call_llm", side_effect=_fake_llm):
            result = await _msg_task(
                config, db, "sess1", "Answer in English. Tell the user the weather.",
                plan_outputs=plan_outputs, goal="weather in Rome",
                user_message="che tempo fa a Roma?",
            )

        # Messenger should receive filtered context
        messenger_content = captured_messages[1]["content"]
        # Relevant outputs present
        assert "weather rome" in messenger_content or "Sunny" in messenger_content
        # Briefer context replaces raw summary/facts
        assert "## Context\nUser asked about weather in Rome." in messenger_content


# --- M269: Retry on empty LLM response ---


class TestM269RetryOnLLMError:
    """M269: _retry_llm_with_validation retries on LLMError instead of crashing."""

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )

    async def test_recovers_after_transient_llm_error(self, config):
        """LLMError on first call, valid JSON on second → succeeds."""
        from kiso.llm import LLMError
        call_count = [0]
        valid_plan = json.dumps({
            "goal": "test", "secrets": None,
            "tasks": [{"type": "msg", "detail": "hi", "skill": None, "args": None, "expect": None}],
        })

        async def _flaky(cfg, role, messages, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMError("Empty response from LLM (planner, deepseek-v3.2)")
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_flaky):
            result = await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
            )
        assert result["goal"] == "test"
        assert call_count[0] == 2

    async def test_exhausts_retries_on_persistent_llm_error(self, config):
        """LLMError on ALL attempts → raises PlanError after exhaustion."""
        from kiso.llm import LLMError

        async def _always_fail(cfg, role, messages, **kw):
            raise LLMError("Empty response")

        with patch("kiso.brain.call_llm", side_effect=_always_fail):
            with pytest.raises(PlanError, match="LLM call failed after 3 attempts"):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, lambda p: [], PlanError, "Plan",
                )

    async def test_llm_error_retries_cleanly_without_feedback(self, config):
        """LLMError retry is a clean retry — no error feedback appended to messages."""
        from kiso.llm import LLMError
        call_count = [0]
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })
        captured_messages: list[list[dict]] = []

        async def _capture(cfg, role, messages, **kw):
            captured_messages.append(list(messages))
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMError("timeout")
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
            )
        # Second call should have identical messages (no feedback, no assistant msg)
        assert len(captured_messages[0]) == len(captured_messages[1])

    async def test_llm_error_preserves_last_errors_on_exhaustion(self, config):
        """exc.last_errors is set when LLM errors exhaust all attempts (M195 compat)."""
        from kiso.llm import LLMError

        async def _always_fail(cfg, role, messages, **kw):
            raise LLMError("Empty response")

        with patch("kiso.brain.call_llm", side_effect=_always_fail):
            with pytest.raises(PlanError) as exc_info:
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, lambda p: [], PlanError, "Plan",
                )
        assert hasattr(exc_info.value, "last_errors")


class TestM308FallbackModel:
    """M308: _retry_llm_with_validation switches to fallback_model when primary exhausts LLM retries."""

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3, max_llm_retries=2),
            raw={},
        )

    async def test_switches_to_fallback_after_primary_exhausted(self, config):
        """After 2 LLM errors, switches to fallback model and succeeds."""
        call_count = [0]
        models_seen: list[str | None] = []
        valid_plan = json.dumps({
            "goal": "test", "secrets": None,
            "tasks": [{"type": "msg", "detail": "hi", "skill": None, "args": None, "expect": None}],
        })

        async def _mock_llm(cfg, role, messages, model_override=None, **kw):
            call_count[0] += 1
            models_seen.append(model_override)
            if call_count[0] <= 2:
                raise LLMError("Empty response")
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_mock_llm):
            result = await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
                fallback_model="fallback-model-v1",
            )

        assert result["goal"] == "test"
        assert call_count[0] == 3
        # First 2 calls used primary (None override), 3rd used fallback
        assert models_seen[:2] == [None, None]
        assert models_seen[2] == "fallback-model-v1"

    async def test_fallback_also_fails_raises_error(self, config):
        """If fallback model also exhausts retries, raises PlanError."""
        async def _always_fail(cfg, role, messages, **kw):
            raise LLMError("Empty response")

        with patch("kiso.brain.call_llm", side_effect=_always_fail):
            with pytest.raises(PlanError, match="LLM call failed after 2 attempts"):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, lambda p: [], PlanError, "Plan",
                    fallback_model="fallback-model-v1",
                )

    async def test_no_fallback_raises_normally(self, config):
        """Without fallback_model, exhaustion raises immediately."""
        async def _always_fail(cfg, role, messages, **kw):
            raise LLMError("Empty response")

        with patch("kiso.brain.call_llm", side_effect=_always_fail):
            with pytest.raises(PlanError, match="LLM call failed after 2 attempts"):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, lambda p: [], PlanError, "Plan",
                )

    async def test_fallback_not_used_when_primary_succeeds(self, config):
        """If primary model succeeds, fallback is never used."""
        models_seen: list[str | None] = []
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _ok(cfg, role, messages, model_override=None, **kw):
            models_seen.append(model_override)
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_ok):
            await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
                fallback_model="fallback-model-v1",
            )

        assert models_seen == [None]  # only primary model used

    async def test_on_retry_callback_notified_of_fallback_switch(self, config):
        """on_retry is called with fallback switch message."""
        call_count = [0]
        retry_reasons: list[str] = []
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _mock_llm(cfg, role, messages, **kw):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise LLMError("Empty response")
            return valid_plan

        def _on_retry(attempt, max_attempts, reason):
            retry_reasons.append(reason)

        with patch("kiso.brain.call_llm", side_effect=_mock_llm):
            await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
                fallback_model="fb-model",
                on_retry=_on_retry,
            )

        assert any("fallback" in r.lower() for r in retry_reasons)


class TestM309ReplanContextDedup:
    """M309: build_planner_messages excludes system_env from context_pool on replan,
    and run_planner passes is_replan to validate_plan preserving extend_replan."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_system_env_excluded_from_context_pool_on_replan(self, db):
        """On replan, system_env is removed from context_pool before briefer."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(),
            settings=_full_settings(briefer_enabled=True),
            raw={},
        )
        captured_pool: list[dict] = []

        async def _mock_briefer(cfg, role, msg, pool, **kw):
            captured_pool.append(dict(pool))
            return {"modules": ["core"], "tools": [], "context": "ctx",
                    "output_indices": [], "relevant_tags": [], "relevant_entities": []}

        with patch("kiso.brain.run_briefer", side_effect=_mock_briefer), \
             patch("kiso.brain.discover_tools", return_value=[]):
            await build_planner_messages(
                db, config, "sess1", "user", "test msg", is_replan=True,
            )

        assert len(captured_pool) == 1
        assert "system_env" not in captured_pool[0]

    async def test_system_env_present_on_initial_plan(self, db):
        """On initial plan, system_env is included in context_pool."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(),
            settings=_full_settings(briefer_enabled=True),
            raw={},
        )
        captured_pool: list[dict] = []

        async def _mock_briefer(cfg, role, msg, pool, **kw):
            captured_pool.append(dict(pool))
            return {"modules": ["core"], "tools": [], "context": "ctx",
                    "output_indices": [], "relevant_tags": [], "relevant_entities": []}

        with patch("kiso.brain.run_briefer", side_effect=_mock_briefer), \
             patch("kiso.brain.discover_tools", return_value=[]):
            await build_planner_messages(
                db, config, "sess1", "user", "test msg", is_replan=False,
            )

        assert len(captured_pool) == 1
        assert "system_env" in captured_pool[0]

    async def test_run_planner_passes_is_replan_to_validate(self):
        """run_planner(is_replan=True) passes is_replan to validate_plan,
        preserving extend_replan in the plan."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(),
            settings=_full_settings(briefer_enabled=False),
            raw={},
        )
        plan_with_extend = json.dumps({
            "goal": "test", "secrets": None, "extend_replan": 2,
            "tasks": [{"type": "msg", "detail": "hi", "skill": None, "args": None, "expect": None}],
        })

        async def _mock_llm(cfg, role, messages, **kw):
            return plan_with_extend

        async def _mock_build(db, cfg, sess, role, msg, **kw):
            return [{"role": "user", "content": "test"}], [], []

        with patch("kiso.brain.build_planner_messages", side_effect=_mock_build) as mock_build, \
             patch("kiso.brain.call_llm", side_effect=_mock_llm):
            result = await run_planner(
                None, config, "sess1", "user", "test msg", is_replan=True,
            )

        # is_replan=True means extend_replan is preserved
        assert result.get("extend_replan") == 2
        # verify build_planner_messages received is_replan=True
        _, kwargs = mock_build.call_args
        assert kwargs.get("is_replan") is True


# --- M272: Briefer omits irrelevant sections for messenger/worker ---


class TestM272BrieferSimpleConsumers:
    """M272: build_briefer_messages omits modules/skills/sys_env for messenger/worker."""

    def _pool(self):
        return {
            "tools": "browser: navigate websites",
            "system_env": "OS: Linux\nArch: x86_64",
            "connectors": "slack: send messages",
            "summary": "User asked about guidance.studio",
            "plan_outputs": "Output 1: page loaded",
            "recent_messages": "[user] vai su guidance.studio",
        }

    def test_planner_gets_all_sections(self):
        """Planner briefer includes Available Modules, skills, sys_env."""
        msgs = build_briefer_messages("planner", "plan the task", self._pool())
        content = msgs[1]["content"]
        assert "Available Modules" in content
        assert "Available Tools" in content
        assert "System Environment" in content

    def test_messenger_omits_modules_and_irrelevant_sections(self):
        """Messenger briefer skips modules, skills, sys_env, connectors."""
        msgs = build_briefer_messages("messenger", "tell user", self._pool())
        content = msgs[1]["content"]
        assert "Available Modules" not in content
        assert "Available Tools" not in content
        assert "System Environment" not in content
        assert "Available Connectors" not in content
        # Relevant sections still present
        assert "Session Summary" in content
        assert "Plan Outputs" in content
        assert "Recent Messages" in content

    def test_worker_omits_modules_and_irrelevant_sections(self):
        """Worker briefer skips modules, skills, sys_env, connectors."""
        msgs = build_briefer_messages("worker", "translate cmd", self._pool())
        content = msgs[1]["content"]
        assert "Available Modules" not in content
        assert "Available Tools" not in content
        # Worker keeps plan_outputs (needed for command context)
        assert "Plan Outputs" in content


# --- M274: no Italian keywords in fallback path ---


@pytest.mark.asyncio()
class TestM274NoItalianKeywords:
    """M274: keyword fallback path uses only English keywords."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    def _config(self):
        return _make_brain_config()

    async def test_utente_does_not_trigger_user_mgmt(self, db):
        """Italian 'utente' no longer triggers user_mgmt module."""
        fake_skills = [{"name": "s1", "version": "1.0", "summary": "x", "commands": {}}]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "crea un utente nuovo",
            )
        system = msgs[0]["content"]
        assert "PROTECTION" not in system

    async def test_installa_does_not_trigger_plugin_install(self, db):
        """Italian 'installa' no longer triggers plugin_install module."""
        fake_skills = [{"name": "s1", "version": "1.0", "summary": "x", "commands": {}}]
        with patch("kiso.brain.discover_tools", return_value=fake_skills):
            msgs, *_ = await build_planner_messages(
                db, self._config(), "test-session", "admin",
                "installa il browser",
            )
        system = msgs[0]["content"]
        assert "Plugin installation:" not in system

    async def test_english_install_still_works(self, db):
        """English 'install' still triggers plugin_install module."""
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin",
            "install the browser connector",
        )
        system = msgs[0]["content"]
        assert "Plugin installation:" in system

    async def test_english_user_still_works(self, db):
        """English 'user' still triggers user_mgmt module."""
        msgs, *_ = await build_planner_messages(
            db, self._config(), "test-session", "admin",
            "add a new user bob",
        )
        system = msgs[0]["content"]
        assert "PROTECTION" in system or "Caller Role" in system


# --- Streaming mock helpers for tests that mock _http_client directly ---


class _MockStreamResp:
    """Mock httpx streaming response with SSE lines."""
    def __init__(self, status_code, sse_lines=None):
        self.status_code = status_code
        self._sse_lines = sse_lines or []

    async def aiter_lines(self):
        for line in self._sse_lines:
            yield line

    async def aread(self):
        return b""


class _BrainStreamCM:
    """Async context manager wrapping a mock stream response."""
    def __init__(self, response):
        self._resp = response

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


def _brain_stream_cm(content: str, usage: dict | None = None) -> _BrainStreamCM:
    """Build a streaming mock context manager for a successful LLM call."""
    lines = []
    if content:
        lines.append(f'data: {json.dumps({"choices": [{"delta": {"content": content}, "index": 0}]})}')
    final: dict = {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]}
    if usage:
        final["usage"] = usage
    lines.append(f"data: {json.dumps(final)}")
    lines.append("data: [DONE]")
    return _BrainStreamCM(_MockStreamResp(200, lines))


# --- M298: No timeout partitioning — each attempt uses full role timeout ---


class TestM298NoTimeoutPartitioning:
    """M298: _retry_llm_with_validation does NOT partition timeout across retries."""

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )

    async def test_no_timeout_override_passed(self, config):
        """call_llm is called without timeout_override (uses role default)."""
        captured_kwargs: list[dict] = []
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _capture(cfg, role, messages, **kw):
            captured_kwargs.append(kw)
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
            )
        assert "timeout_override" not in captured_kwargs[0]

    async def test_call_llm_uses_unified_timeout(self):
        """M422: all roles use llm_timeout (no per-role overrides)."""
        from kiso.llm import call_llm
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(llm_timeout=250),
            raw={},
        )
        plan_content = '{"goal":"x","secrets":null,"tasks":[{"type":"msg","detail":"d","skill":null,"args":null,"expect":null}]}'
        with patch("kiso.llm._http_client") as mock_client:
            mock_client.stream = MagicMock(return_value=_brain_stream_cm(plan_content))
            await call_llm(
                config, "planner",
                [{"role": "user", "content": "test"}],
                response_format=PLAN_SCHEMA,
            )
            call_kwargs = mock_client.stream.call_args[1]
            assert call_kwargs["timeout"] == 250  # unified llm_timeout

    async def test_retry_fires_on_timeout(self, config):
        """When first attempt times out, retry fires (each attempt gets full timeout)."""
        call_count = [0]
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _timeout_then_ok(cfg, role, messages, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMError("LLM call timed out (planner, gpt-4)")
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_timeout_then_ok):
            result = await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
            )
        assert result["goal"] == "ok"
        assert call_count[0] == 2


# --- M296: Per-role max_tokens defaults ---


class TestM296MaxTokensDefaults:
    """M296: call_llm applies per-role max_tokens defaults."""

    def _config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(worker="gpt-4"),
            settings=_full_settings(),
            raw={},
        )

    async def test_default_max_tokens_applied(self):
        """Worker role gets max_tokens=500 from MAX_TOKENS_DEFAULTS."""
        from kiso.llm import call_llm
        from kiso.config import MAX_TOKENS_DEFAULTS
        config = self._config()
        with patch("kiso.llm._http_client") as mock_client:
            mock_client.stream = MagicMock(return_value=_brain_stream_cm("ls"))
            await call_llm(config, "worker", [{"role": "user", "content": "test"}])
            payload = mock_client.stream.call_args[1]["json"]
            assert payload["max_tokens"] == MAX_TOKENS_DEFAULTS["worker"]

    async def test_explicit_max_tokens_overrides_default(self):
        """Explicit max_tokens parameter overrides the role default."""
        from kiso.llm import call_llm
        config = self._config()
        with patch("kiso.llm._http_client") as mock_client:
            mock_client.stream = MagicMock(return_value=_brain_stream_cm("ls"))
            await call_llm(
                config, "worker",
                [{"role": "user", "content": "test"}],
                max_tokens=999,
            )
            payload = mock_client.stream.call_args[1]["json"]
            assert payload["max_tokens"] == 999

    def test_all_roles_have_max_tokens_default(self):
        """Every role in MODEL_DEFAULTS has a corresponding MAX_TOKENS_DEFAULTS entry."""
        from kiso.config import MAX_TOKENS_DEFAULTS, MODEL_DEFAULTS
        for role in MODEL_DEFAULTS:
            assert role in MAX_TOKENS_DEFAULTS, f"Missing max_tokens default for role: {role}"


# --- M297: Retry status notification ---


class TestM297RetryNotification:
    """M297: on_retry callback fires before each retry, not on first attempt."""

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_validation_retries=3),
            raw={},
        )

    async def test_on_retry_called_on_llm_error(self, config):
        """on_retry fires before retry after LLMError, not on first attempt."""
        retry_calls: list[tuple] = []
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })
        call_count = [0]

        async def _flaky(cfg, role, messages, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMError("timeout")
            return valid_plan

        def _on_retry(attempt, max_attempts, reason):
            retry_calls.append((attempt, max_attempts, reason))

        with patch("kiso.brain.call_llm", side_effect=_flaky):
            await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
                on_retry=_on_retry,
            )
        assert len(retry_calls) == 1
        assert retry_calls[0][0] == 2  # attempt 2
        assert retry_calls[0][1] == 6  # max_total = max_llm_retries(3) + max_validation_retries(3)
        assert "timeout" in retry_calls[0][2]

    async def test_on_retry_not_called_on_success(self, config):
        """on_retry is never called when first attempt succeeds."""
        retry_calls: list[tuple] = []
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _ok(cfg, role, messages, **kw):
            return valid_plan

        def _on_retry(attempt, max_attempts, reason):
            retry_calls.append((attempt, max_attempts, reason))

        with patch("kiso.brain.call_llm", side_effect=_ok):
            await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
                on_retry=_on_retry,
            )
        assert len(retry_calls) == 0

    async def test_on_retry_none_is_safe(self, config):
        """on_retry=None (default) doesn't crash on retry."""
        call_count = [0]
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _flaky(cfg, role, messages, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMError("timeout")
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_flaky):
            result = await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
            )
        assert result["goal"] == "ok"

    async def test_error_message_includes_attempt_count(self, config):
        """Final error message includes the retry count."""
        async def _always_fail(cfg, role, messages, **kw):
            raise LLMError("timeout")

        with patch("kiso.brain.call_llm", side_effect=_always_fail):
            with pytest.raises(PlanError, match="after 3 attempts"):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, lambda p: [], PlanError, "Plan",
                )


# --- M302: Integration tests — stall simulation, retry separation ---


class TestM302StallRetryIntegration:
    """M302: end-to-end stall detection + separate retry budgets."""

    async def test_stall_detected_and_retry_succeeds(self):
        """Mock server sends 2 chunks then stalls → stall detected → retry succeeds."""
        import asyncio
        from kiso.llm import LLMStallError

        call_count = [0]
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _stall_then_ok(cfg, role, messages, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMStallError("LLM stream stalled (no data for 60s)")
            return valid_plan

        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_llm_retries=3, max_validation_retries=3),
            raw={},
        )
        with patch("kiso.brain.call_llm", side_effect=_stall_then_ok):
            result = await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
            )
        assert result["goal"] == "ok"
        assert call_count[0] == 2

    async def test_llm_budget_exhausted_separately(self):
        """LLM retry budget exhausted independently from validation budget."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_llm_retries=2, max_validation_retries=5),
            raw={},
        )

        async def _always_timeout(cfg, role, messages, **kw):
            raise LLMError("timeout")

        with patch("kiso.brain.call_llm", side_effect=_always_timeout):
            with pytest.raises(PlanError, match="after 2 attempts"):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, lambda p: [], PlanError, "Plan",
                )

    async def test_validation_budget_exhausted_separately(self):
        """Validation retry budget exhausted independently from LLM budget."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_llm_retries=5, max_validation_retries=2),
            raw={},
        )

        async def _bad_json(cfg, role, messages, **kw):
            return "not valid json"

        with patch("kiso.brain.call_llm", side_effect=_bad_json):
            with pytest.raises(PlanError, match="validation failed after 2"):
                await _retry_llm_with_validation(
                    config, "planner",
                    [{"role": "user", "content": "test"}],
                    PLAN_SCHEMA, lambda p: ["bad"], PlanError, "Plan",
                )

    async def test_on_retry_fires_for_both_types(self):
        """on_retry callback receives calls for both LLM and validation errors."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_llm_retries=3, max_validation_retries=3),
            raw={},
        )
        retry_calls: list[tuple] = []
        call_count = [0]
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _mixed_failures(cfg, role, messages, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMError("stall timeout")
            if call_count[0] == 2:
                return "invalid json{{"
            return valid_plan

        def _on_retry(attempt, max_attempts, reason):
            retry_calls.append((attempt, max_attempts, reason))

        with patch("kiso.brain.call_llm", side_effect=_mixed_failures):
            result = await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
                on_retry=_on_retry,
            )
        assert result["goal"] == "ok"
        assert len(retry_calls) == 2  # one for LLM error, one for JSON error
        assert "stall" in retry_calls[0][2].lower()
        assert "json" in retry_calls[1][2].lower()

    async def test_full_timeout_not_partitioned(self):
        """Each LLM attempt uses the full role timeout (no partitioning)."""
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
            users={},
            models=_full_models(planner="gpt-4"),
            settings=_full_settings(max_llm_retries=3, max_validation_retries=3),
            raw={},
        )
        captured_kwargs: list[dict] = []
        valid_plan = json.dumps({
            "goal": "ok", "secrets": None,
            "tasks": [{"type": "msg", "detail": "d", "skill": None, "args": None, "expect": None}],
        })

        async def _capture(cfg, role, messages, **kw):
            captured_kwargs.append(kw)
            return valid_plan

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await _retry_llm_with_validation(
                config, "planner",
                [{"role": "user", "content": "test"}],
                PLAN_SCHEMA, lambda p: validate_plan(p), PlanError, "Plan",
            )
        # No timeout_override should be passed (removed in M298)
        assert "timeout_override" not in captured_kwargs[0]


# --- M304: Briefer skip module validation for simple consumers ---


class TestM304BrieferModuleValidationSkip:
    """M304: validate_briefing skips module name check for simple consumers."""

    def test_check_modules_true_rejects_unknown(self):
        """Default: unknown modules are rejected."""
        briefing = {
            "modules": ["nonexistent"],
            "tools": [], "context": "", "output_indices": [], "relevant_tags": [],
            "relevant_entities": [],
        }
        errors = validate_briefing(briefing, check_modules=True)
        assert any("nonexistent" in e for e in errors)

    def test_check_modules_false_accepts_unknown(self):
        """With check_modules=False, any module names pass validation."""
        briefing = {
            "modules": ["hallucinated_module", "another_fake"],
            "tools": [], "context": "", "output_indices": [], "relevant_tags": [],
            "relevant_entities": [],
        }
        errors = validate_briefing(briefing, check_modules=False)
        assert errors == []

    def test_check_modules_false_still_validates_type(self):
        """Even with check_modules=False, modules must be an array."""
        briefing = {
            "modules": "not_a_list",
            "tools": [], "context": "", "output_indices": [], "relevant_tags": [],
            "relevant_entities": [],
        }
        errors = validate_briefing(briefing, check_modules=False)
        assert any("modules must be an array" in e for e in errors)

    def test_check_modules_false_still_validates_other_fields(self):
        """check_modules=False doesn't skip validation of other fields."""
        briefing = {
            "modules": ["whatever"],
            "tools": "not_a_list",  # invalid
            "context": None,  # invalid
            "output_indices": [], "relevant_tags": [],
            "relevant_entities": [],
        }
        errors = validate_briefing(briefing, check_modules=False)
        assert any("tools" in e for e in errors)
        assert any("context" in e for e in errors)


@pytest.mark.asyncio()
class TestM304RunBrieferSimpleConsumers:
    """M304: run_briefer skips module validation and forces modules=[] for messenger/worker."""

    @pytest.fixture()
    def config(self):
        return Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="http://localhost")},
            users={},
            models=_full_models(),
            settings=_full_settings(),
            raw={},
        )

    async def test_messenger_accepts_hallucinated_modules(self, config):
        """Messenger briefer doesn't retry on hallucinated module names."""
        response = json.dumps({
            "modules": ["install_skill", "navigate_and_summarize"],
            "tools": [], "context": "About to install browser", "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "messenger", "tell user", {})
        # Hallucinated modules accepted (no retry), then forced to []
        assert result["modules"] == []
        assert result["context"] == "About to install browser"

    async def test_worker_accepts_hallucinated_modules(self, config):
        """Worker briefer doesn't retry on hallucinated module names."""
        response = json.dumps({
            "modules": ["BrowserSkill"],
            "tools": [], "context": "", "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            result = await run_briefer(config, "worker", "translate cmd", {})
        assert result["modules"] == []

    async def test_planner_still_validates_modules(self, config):
        """Planner briefer still rejects unknown module names."""
        response = json.dumps({
            "modules": ["nonexistent_module"],
            "tools": [], "context": "", "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=response):
            with pytest.raises(BrieferError):
                await run_briefer(config, "planner", "plan task", {})

    async def test_messenger_single_call(self, config):
        """Messenger briefer with hallucinated modules uses exactly 1 LLM call."""
        response = json.dumps({
            "modules": ["fake_module"],
            "tools": [], "context": "test", "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        })
        mock_llm = AsyncMock(return_value=response)
        with patch("kiso.brain.call_llm", mock_llm):
            await run_briefer(config, "messenger", "tell user", {})
        assert mock_llm.call_count == 1  # no retries


# ---------------------------------------------------------------------------
# M406 — In-flight message classifier
# ---------------------------------------------------------------------------


class TestBuildInflightClassifierMessages:
    def test_basic_structure(self):
        """build_inflight_classifier_messages returns a user message."""
        msgs = build_inflight_classifier_messages("deploy to staging", "usa porta 8080")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "deploy to staging" in msgs[0]["content"]
        assert "usa porta 8080" in msgs[0]["content"]

    def test_contains_all_categories(self):
        """Prompt mentions all four inflight categories."""
        msgs = build_inflight_classifier_messages("goal", "msg")
        text = msgs[0]["content"]
        for cat in INFLIGHT_CATEGORIES:
            assert cat in text


class TestClassifyInflight:
    async def test_returns_stop(self):
        """classify_inflight returns 'stop' when LLM says 'stop'."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="stop"):
            result = await classify_inflight(config, "deploy app", "fermati")
        assert result == "stop"

    async def test_returns_update(self):
        """classify_inflight returns 'update' for parameter changes."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="update"):
            result = await classify_inflight(config, "deploy app", "usa porta 8080")
        assert result == "update"

    async def test_returns_independent(self):
        """classify_inflight returns 'independent' for unrelated messages."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="independent"):
            result = await classify_inflight(config, "deploy app", "che ore sono?")
        assert result == "independent"

    async def test_returns_conflict(self):
        """classify_inflight returns 'conflict' for contradicting messages."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="conflict"):
            result = await classify_inflight(config, "deploy app", "no fai X invece")
        assert result == "conflict"

    async def test_strips_whitespace(self):
        """classify_inflight handles LLM output with whitespace."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="  stop\n"):
            result = await classify_inflight(config, "goal", "msg")
        assert result == "stop"

    async def test_case_insensitive(self):
        """classify_inflight handles uppercase responses."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value="STOP"):
            result = await classify_inflight(config, "goal", "msg")
        assert result == "stop"

    async def test_unexpected_output_falls_back_to_independent(self):
        """classify_inflight returns 'independent' for unexpected LLM output."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    return_value="I think this is a stop"):
            result = await classify_inflight(config, "goal", "msg")
        assert result == "independent"

    async def test_llm_error_falls_back_to_independent(self):
        """classify_inflight returns 'independent' when LLM call fails."""
        config = _make_config_for_classifier()
        with patch("kiso.brain.call_llm", new_callable=AsyncMock,
                    side_effect=LLMError("timeout")):
            result = await classify_inflight(config, "goal", "msg")
        assert result == "independent"

    async def test_uses_classifier_role(self):
        """classify_inflight should call LLM with 'classifier' role."""
        config = _make_config_for_classifier()
        mock_llm = AsyncMock(return_value="stop")
        with patch("kiso.brain.call_llm", mock_llm):
            await classify_inflight(config, "goal", "msg", session="s1")
        mock_llm.assert_called_once()
        assert mock_llm.call_args[0][1] == "classifier"
        assert mock_llm.call_args[1].get("session") == "s1"


class TestInflightCategories:
    def test_contains_expected_values(self):
        """INFLIGHT_CATEGORIES contains all four expected values."""
        assert INFLIGHT_CATEGORIES == {"stop", "update", "independent", "conflict"}


# ---------------------------------------------------------------------------
# M407 — Stop pattern fast-path
# ---------------------------------------------------------------------------


class TestIsStopMessage:
    @pytest.mark.parametrize("text", [
        "stop", "STOP", "ferma", "ferma!", "fermati", "Annulla",
        "cancel", "abort", "basta", "quit", "stop!", "STOP!",
        "basta.", "  ferma  ", "FERMATI",
    ])
    def test_matches_stop_words(self, text):
        """Single stop words (with optional trailing punctuation) match."""
        assert is_stop_message(text) is True

    @pytest.mark.parametrize("text", [
        "FERMATI ORA", "STOP NOW", "BASTA!", "AIUTO",
    ])
    def test_matches_urgent_caps(self, text):
        """ALL-CAPS messages ≥4 chars match as urgent."""
        assert is_stop_message(text) is True

    @pytest.mark.parametrize("text", [
        "stop using port 80", "cancel the deploy and use 8080",
        "fermati dopo il secondo task", "quit smoking",
        "hello", "deploy to staging", "che ore sono?",
    ])
    def test_no_match_with_content(self, text):
        """Messages with content after the stop word do NOT match."""
        assert is_stop_message(text) is False

    def test_empty_string(self):
        assert is_stop_message("") is False

    def test_short_caps(self):
        """ALL-CAPS under 4 chars should not match."""
        assert is_stop_message("NO") is False
        assert is_stop_message("OK") is False
