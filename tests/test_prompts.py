"""M287: Prompt content integration tests.

Verify that critical rules survive prompt edits across all role files.
These are string-content tests, not LLM tests — they guard against
accidental removal of important instructions during refactoring.
"""

from pathlib import Path

import pytest

_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"

# All role prompt files that should exist
_EXPECTED_ROLES = [
    "planner.md",
    "reviewer.md",
    "messenger.md",
    "worker.md",
    "classifier.md",
    "briefer.md",
    "curator.md",
    "searcher.md",
    "summarizer-session.md",
    "summarizer-facts.md",
    "paraphraser.md",
]


class TestAllPromptsLoadable:
    """Every role prompt file must exist and be non-empty."""

    @pytest.mark.parametrize("filename", _EXPECTED_ROLES)
    def test_role_file_exists_and_nonempty(self, filename):
        path = _ROLES_DIR / filename
        assert path.is_file(), f"Missing role file: {filename}"
        content = path.read_text()
        assert len(content.strip()) > 20, f"Role file too short: {filename}"


class TestPlannerCriticalRules:
    """Critical planner rules that must not be removed."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "planner.md").read_text()

    def test_browser_workflow_via_usage_guide(self):
        """M275: planner follows skill usage guides."""
        assert "guide:" in self.prompt.lower()
        assert "follow" in self.prompt.lower()

    def test_language_universal(self):
        """M286: planner accepts any language."""
        assert "any language" in self.prompt
        assert "any script" in self.prompt

    def test_msg_language_prefix(self):
        assert "Answer in {language}" in self.prompt

    def test_kiso_native_first(self):
        assert "Kiso-native first" in self.prompt

    def test_no_fabricate(self):
        assert "fabricate" in self.prompt.lower()

    def test_replan_last(self):
        assert "Replan must always be last" in self.prompt

    def test_language_handling(self):
        """M286: explicit language handling rule."""
        assert "Msg detail:" in self.prompt

    def test_m328_web_module_browser_state(self):
        """M328: web module mentions browser state persistence."""
        assert "Browser state persists" in self.prompt

    def test_m328_web_module_captcha(self):
        """M328: web module has CAPTCHA awareness."""
        assert "CAPTCHA" in self.prompt
        assert "human verification" in self.prompt

    def test_m328_web_module_text_action(self):
        """M328: web module prefers browser text action."""
        assert "browser `text` action" in self.prompt


class TestMessengerCriticalRules:
    """Critical messenger rules that must not be removed."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "messenger.md").read_text()

    def test_voice_rules(self):
        """M277/M424: explicit voice rules."""
        assert "Voice rules" in self.prompt
        assert '"I ran"' in self.prompt

    def test_m327_upcoming_actions_first_person(self):
        """M327: upcoming actions use first person, not third person."""
        assert "Upcoming actions: first person" in self.prompt
        assert "The user sees you as one entity" in self.prompt

    def test_language_consolidated(self):
        """M424: consolidated language block."""
        assert "Language:" in self.prompt
        assert "one language" in self.prompt

    def test_no_fabricate(self):
        assert "fabricate" in self.prompt.lower()

    def test_answer_in_language(self):
        assert "Answer in {language}" in self.prompt

    def test_verbatim(self):
        assert "verbatim" in self.prompt


class TestClassifierCriticalRules:
    """Critical classifier rules that must not be removed."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "classifier.md").read_text()

    def test_three_categories(self):
        assert '"plan"' in self.prompt
        assert '"chat_kb"' in self.prompt
        assert '"chat"' in self.prompt

    def test_safe_fallback(self):
        assert "doubt" in self.prompt.lower()

    def test_recent_context(self):
        """M276: follow-up context handling."""
        assert "Recent Context" in self.prompt
        assert "follow-up" in self.prompt.lower() or "follow up" in self.prompt.lower()

    def test_any_language(self):
        assert "any language" in self.prompt


class TestReviewerCriticalRules:
    """Critical reviewer rules that must not be removed."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "reviewer.md").read_text()

    def test_expect_is_sole_criterion(self):
        assert "Sole criterion is `expect`" in self.prompt

    def test_summary_field(self):
        assert "summary" in self.prompt

    def test_truncated_output(self):
        """M280: truncation handling."""
        assert "[truncated]" in self.prompt

    def test_partial_success(self):
        """M280: partial success."""
        assert "Partial success" in self.prompt

    def test_domain_check(self):
        assert "wrong domain" in self.prompt

    def test_m329_browser_fill_tolerance(self):
        """M329: reviewer tolerates browser fill confirmation."""
        assert "Browser fill actions" in self.prompt
        assert "tool confirmed the fill" in self.prompt

    def test_m359_self_entity_hint(self):
        """M359: reviewer learn hints 'This Kiso instance' for self-entity."""
        assert "This Kiso instance" in self.prompt
        assert "entity" in self.prompt.lower()


class TestBrieferCriticalRules:
    """Critical briefer rules that must not be removed."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "briefer.md").read_text()

    def test_aggressive_filtering(self):
        assert "AGGRESSIVE" in self.prompt

    def test_fast_path(self):
        """M281: fast-path examples."""
        assert "Fast-path" in self.prompt

    def test_conflict_handling(self):
        """M281: conflicting facts."""
        assert "Conflicting facts" in self.prompt

    def test_messenger_no_modules(self):
        assert "For messenger/worker: modules=[] and tools=[] always" in self.prompt

    def test_m357_self_entity_routing(self):
        """M357: briefer routes entity 'self' for system introspection."""
        assert 'Entity "self"' in self.prompt
        assert "this Kiso instance" in self.prompt


class TestWorkerCriticalRules:
    """Critical worker rules that must not be removed."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "worker.md").read_text()

    def test_retry_hint_priority(self):
        """M284: retry hint is absolute priority."""
        assert "ABSOLUTE priority" in self.prompt

    def test_cannot_translate(self):
        assert "CANNOT_TRANSLATE" in self.prompt

    def test_no_sudo(self):
        assert "sudo" in self.prompt

    def test_tool_path(self):
        """M284: tool venv PATH."""
        assert "Tool binaries" in self.prompt


class TestCuratorCriticalRules:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "curator.md").read_text()

    def test_tag_reuse(self):
        """M282: tag reuse enforcement."""
        assert "Tag reuse" in self.prompt

    def test_contradiction(self):
        """M282: contradicting facts."""
        assert "Contradicting facts" in self.prompt

    def test_no_secrets(self):
        assert "secrets" in self.prompt.lower()

    def test_m343_entity_assignment(self):
        """M343: curator has entity_name + entity_kind assignment rules."""
        assert "entity_name" in self.prompt
        assert "entity_kind" in self.prompt
        assert "Entity reuse" in self.prompt

    def test_m359_self_entity_rule(self):
        """M359: curator assigns entity 'self' for system learnings."""
        assert 'Entity "self"' in self.prompt
        assert 'entity_name="self"' in self.prompt
        assert 'entity_kind="system"' in self.prompt


class TestSearcherCriticalRules:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "searcher.md").read_text()

    def test_lang_matching(self):
        """M283: output language matches query."""
        assert "query language controls output language" in self.prompt

    def test_source_quality(self):
        """M283: prefer primary sources."""
        assert "primary sources" in self.prompt.lower()


class TestSummarizerCriticalRules:
    def test_session_english(self):
        """M285: session summary in English."""
        prompt = (_ROLES_DIR / "summarizer-session.md").read_text()
        assert "English" in prompt

    def test_facts_english(self):
        """M285: facts in English."""
        prompt = (_ROLES_DIR / "summarizer-facts.md").read_text()
        assert "English" in prompt


class TestM316PromptOptimizationIntegration:
    """M316: Verify prompt optimization preserved all modules and kept sizes reasonable."""

    _ALL_MODULES = [
        "core", "kiso_native", "planning_rules", "tools_rules",
        "tool_recovery", "data_flow", "web", "scripting", "replan",
        "kiso_commands", "user_mgmt", "plugin_install",
    ]

    def test_all_12_planner_modules_parseable(self):
        """All 12 planner modules must be individually loadable."""
        from kiso.brain import _load_modular_prompt
        for mod in self._ALL_MODULES:
            if mod == "core":
                continue  # core is always included
            result = _load_modular_prompt("planner", [mod])
            assert len(result) > 100, f"Module {mod} produced too little output"

    def test_all_modules_combined(self):
        """Loading all modules together produces a complete prompt."""
        from kiso.brain import _load_modular_prompt
        non_core = [m for m in self._ALL_MODULES if m != "core"]
        result = _load_modular_prompt("planner", non_core)
        for mod in self._ALL_MODULES:
            assert f"<!-- MODULE: {mod} -->" not in result or mod == "core", \
                f"Module marker for {mod} leaked into output"

    def test_planner_prompt_size_regression(self):
        """Planner prompt must stay under 8000 chars (M372 expanded)."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert len(prompt) < 8000, f"Planner prompt too large: {len(prompt)} chars"

    def test_messenger_prompt_size_regression(self):
        """Messenger prompt must stay under 2500 chars (M384 expanded)."""
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert len(prompt) < 2500, f"Messenger prompt too large: {len(prompt)} chars"

    def test_reviewer_prompt_size_regression(self):
        """Reviewer prompt must stay under 3200 chars (M360 optimized)."""
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert len(prompt) < 3200, f"Reviewer prompt too large: {len(prompt)} chars"

    def test_all_role_prompts_nonempty(self):
        """Every role prompt must have substantive content."""
        for filename in _EXPECTED_ROLES:
            content = (_ROLES_DIR / filename).read_text()
            assert len(content.strip()) > 50, f"{filename} has too little content"


class TestM384MessengerAntiHallucination:
    """M384: messenger prompt has verb blocklist and no-emoji rule."""

    def test_no_emoji_rule(self):
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "No emoji" in prompt

    def test_no_false_action_claims(self):
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Never claim to have performed" in prompt

    def test_report_only_outputs(self):
        prompt = (_ROLES_DIR / "messenger.md").read_text()
        assert "Report only what the outputs show" in prompt


class TestM340SkillArgsRequirement:
    """M340: planner prompt emphasizes tool args must never be null."""

    def test_tools_rules_contains_never_null(self):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["tools_rules"])
        assert "Never null" in prompt

    def test_core_contains_args_example(self):
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "JSON string" in prompt


class TestM323LearningPipelineQuality:
    """M323: integration tests for the reviewer→curator learning pipeline."""

    def test_reviewer_enforces_max_3_learns(self):
        """Reviewer schema caps learn array at 3."""
        from kiso.brain import REVIEW_SCHEMA
        learn_schema = REVIEW_SCHEMA["json_schema"]["schema"]["properties"]["learn"]
        array_schema = learn_schema["anyOf"][0]
        assert array_schema["maxItems"] == 3

    def test_reviewer_prompt_self_contained_rule(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "self-contained with subject" in prompt

    def test_reviewer_prompt_consolidation_rule(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "consolidate" in prompt.lower()

    def test_reviewer_prompt_transient_rule(self):
        prompt = (_ROLES_DIR / "reviewer.md").read_text()
        assert "transient" in prompt.lower()

    def test_curator_prompt_consolidation_rule(self):
        prompt = (_ROLES_DIR / "curator.md").read_text()
        assert "consolidat" in prompt.lower()
        assert "learning_id" in prompt

    def test_clean_learn_items_filters_fragmented(self):
        """clean_learn_items + save_learning dedup survive a realistic bad batch."""
        from kiso.brain import clean_learn_items
        # Simulate the kind of garbage the old reviewer produced
        bad_batch = [
            "browser skill installed successfully",
            "The page title is 'Guidance'.",  # too short? no, 32 chars — but transient-ish
            "The contact form includes Name [8], Email [9], and details [10].",
            "A submit button [14] is available for the contact form.",
            "guidance.studio has a contact form with name and email fields",
        ]
        cleaned = clean_learn_items(bad_batch)
        # "installed successfully" filtered, [8] [9] [10] filtered, [14] alone kept
        assert len(cleaned) <= 3
        # The valid self-contained learning survives
        assert any("guidance.studio" in item for item in cleaned)
        # The transient "installed successfully" is gone
        assert not any("installed successfully" in item for item in cleaned)
        # Items with 2+ element indices are gone
        assert not any("[8]" in item and "[9]" in item for item in cleaned)


class TestM362PlannerCLICommandAudit:
    """M362: planner kiso_commands module must match actual CLI entrypoints."""

    def _get_actual_subcommands(self):
        """Extract all subcommands from the actual CLI parser."""
        import argparse as _argparse
        from cli import build_parser
        parser = build_parser()
        result = {}
        for action in parser._subparsers._actions:
            if not isinstance(action, _argparse._SubParsersAction):
                continue
            for name, subparser in action.choices.items():
                subs = []
                if subparser._subparsers is not None:
                    for sub_action in subparser._subparsers._actions:
                        if isinstance(sub_action, _argparse._SubParsersAction):
                            subs = list(sub_action.choices.keys())
                result[name] = sorted(subs)
        return result

    def test_skill_subcommands_match(self):
        """Planner prompt lists all actual skill subcommands."""
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        for sub in actual.get("skill", []):
            assert sub in prompt, f"Missing skill subcommand in prompt: {sub}"

    def test_connector_subcommands_match(self):
        """Planner prompt lists all actual connector subcommands."""
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        for sub in actual.get("connector", []):
            assert sub in prompt, f"Missing connector subcommand in prompt: {sub}"

    def test_env_subcommands_match(self):
        """Planner prompt lists all actual env subcommands."""
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        for sub in actual.get("env", []):
            assert sub in prompt, f"Missing env subcommand in prompt: {sub}"

    def test_user_subcommands_match(self):
        """Planner prompt lists all actual user subcommands."""
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        for sub in actual.get("user", []):
            assert sub in prompt, f"Missing user subcommand in prompt: {sub}"

    def test_reset_subcommands_match(self):
        """Planner prompt lists all actual reset subcommands."""
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        for sub in actual.get("reset", []):
            assert sub in prompt, f"Missing reset subcommand in prompt: {sub}"

    def test_no_phantom_commands(self):
        """Planner prompt does not reference commands that don't exist."""
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        # Verify main command groups mentioned in prompt exist
        for cmd in ["skill", "connector", "env", "user"]:
            assert cmd in actual, f"Prompt mentions '{cmd}' but not in CLI"
        # Ensure 'instance' is NOT in prompt (not a real CLI command)
        assert "kiso instance" not in prompt


class TestM367PlannerOsPackageConfirmation:
    """M367/M419: planner requires user confirmation before ANY install."""

    def test_never_install_anything_rule(self):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_native"])
        assert "Never install anything" in prompt

    def test_user_approval_required(self):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_native"])
        assert "user approval" in prompt.lower()

    def test_web_module_offers_search_alternative(self):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["web"])
        assert "search" in prompt.lower()
        assert "Install first if missing" not in prompt

    def test_tools_rules_ask_user(self):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["tools_rules"])
        assert "msg" in prompt.lower() and "approval" in prompt.lower()

    def test_plugin_install_prerequisite(self):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["plugin_install"])
        assert "approved" in prompt.lower() or "consent" in prompt.lower()


class TestM366PlannerMsgDetailPurity:
    """M366: planner msg detail purity + English enforcement."""

    def test_msg_detail_no_strategy(self):
        """Planner prompt forbids plan strategy in msg detail."""
        prompt = (_ROLES_DIR / "planner.md").read_text()
        assert "Never include plan strategy" in prompt

    def test_msg_detail_no_overview(self):
        """Planner prompt forbids overview/reasoning in msg detail."""
        prompt = (_ROLES_DIR / "planner.md").read_text().lower()
        assert "overview" in prompt or "reasoning" in prompt

    def test_replan_english_enforcement(self):
        """Planner prompt enforces English detail even in replan context."""
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["replan"])
        assert "detail must be in English regardless" in prompt
