"""M287/M595: Prompt smoke tests.

Lightweight guards that role prompt files exist, load, and contain core
terms.  Behavioral validation lives in test_brain.py and test_worker.py —
these only catch accidental file deletion or catastrophic rewrites.

Also includes timeout/prompt-size tests merged from test_timeout_prompts.py (M708).
"""

from pathlib import Path

import pytest

from kiso.brain import _BRIEFER_MODULE_DESCRIPTIONS

_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"

_EXPECTED_ROLES = [
    "planner.md", "reviewer.md", "messenger.md", "worker.md",
    "classifier.md", "briefer.md", "curator.md", "searcher.md",
    "summarizer-session.md", "summarizer-facts.md", "paraphraser.md",
    "inflight-classifier.md",
]

# Core terms per role — if these disappear the LLM behavior will break.
# Only include terms that are structurally essential, not cosmetic phrasing.
_ROLE_CORE_TERMS: list[tuple[str, list[str]]] = [
    ("planner.md", ["json plan", "goal", "tasks", "msg", "replan"]),
    ("reviewer.md", ["ok", "replan", "stuck", "expect", "summary"]),
    ("messenger.md", ["language", "verbatim", "fabricat"]),
    ("worker.md", ["cannot_translate", "sudo"]),
    ("classifier.md", ["plan", "chat"]),
    ("briefer.md", ["context", "modules"]),
    ("curator.md", ["promote", "discard", "entity"]),
    ("searcher.md", ["query"]),
    ("summarizer-session.md", ["english"]),
    ("summarizer-facts.md", ["consolidat"]),
    ("paraphraser.md", ["paraphras"]),
    ("inflight-classifier.md", ["stop", "update", "independent", "conflict"]),
]


@pytest.mark.parametrize("filename", _EXPECTED_ROLES)
def test_role_file_exists_and_nonempty(filename):
    path = _ROLES_DIR / filename
    assert path.is_file(), f"Missing role file: {filename}"
    assert len(path.read_text().strip()) > 20, f"Role file too short: {filename}"


@pytest.mark.parametrize(
    "filename,terms", _ROLE_CORE_TERMS,
    ids=[t[0].replace(".md", "") for t in _ROLE_CORE_TERMS],
)
def test_role_prompt_has_core_terms(filename, terms):
    content = (_ROLES_DIR / filename).read_text().lower()
    for term in terms:
        assert term in content, f"{filename}: missing core term '{term}'"


class TestPromptSizeRegression:
    """Prompt files must not exceed size budgets (token cost guard)."""

    @pytest.mark.parametrize("filename,max_chars", [
        ("planner.md", 9800),
        ("messenger.md", 2500),
        ("reviewer.md", 3200),
    ])
    def test_prompt_size(self, filename, max_chars):
        size = len((_ROLES_DIR / filename).read_text())
        assert size < max_chars, f"{filename}: {size} chars exceeds {max_chars}"


class TestPlannerModules:
    """All planner modules must be individually loadable."""

    _ALL_MODULES = [
        "core", "kiso_native", "planning_rules", "tools_rules",
        "tool_recovery", "data_flow", "web", "scripting", "replan",
        "kiso_commands", "user_mgmt", "plugin_install",
    ]

    @pytest.mark.parametrize("module", [m for m in _ALL_MODULES if m != "core"])
    def test_module_loads(self, module):
        from kiso.brain import _load_modular_prompt
        result = _load_modular_prompt("planner", [module])
        assert len(result) > 100, f"Module {module} too short"


class TestM362CLICommandAudit:
    """Planner kiso_commands module must match actual CLI subcommands."""

    def _get_actual_subcommands(self):
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

    @pytest.mark.parametrize("cmd_group", ["skill", "connector", "env", "user", "reset"])
    def test_subcommands_match(self, cmd_group):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        if cmd_group in actual:
            for sub in actual[cmd_group]:
                assert sub in prompt, f"Missing {cmd_group} subcommand: {sub}"


class TestM323LearningPipeline:
    """Schema + clean_learn_items survive prompt edits."""

    def test_reviewer_schema_max_3_learns(self):
        from kiso.brain import REVIEW_SCHEMA
        learn_schema = REVIEW_SCHEMA["json_schema"]["schema"]["properties"]["learn"]
        assert learn_schema["anyOf"][0]["maxItems"] == 3

    def test_clean_learn_items_filters_garbage(self):
        from kiso.brain import clean_learn_items
        bad_batch = [
            "browser skill installed successfully",
            "The contact form includes Name [8], Email [9], and details [10].",
            "guidance.studio has a contact form with name and email fields",
        ]
        cleaned = clean_learn_items(bad_batch)
        assert len(cleaned) <= 3
        assert any("guidance.studio" in item for item in cleaned)
        assert not any("installed successfully" in item for item in cleaned)


# ---------------------------------------------------------------------------
# Merged from test_timeout_prompts.py (M708)
# ---------------------------------------------------------------------------


class TestTimeoutUnification:
    """M422: per-role timeouts removed, single llm_timeout used."""

    def test_default_config_has_no_per_role_timeouts(self):
        from kiso.config import SETTINGS_DEFAULTS
        assert "planner_timeout" not in SETTINGS_DEFAULTS
        assert "messenger_timeout" not in SETTINGS_DEFAULTS
        assert "llm_timeout" in SETTINGS_DEFAULTS


class TestPromptSizes:
    """Prompt token counts must stay within budget after M423-M425 optimization."""

    def test_planner_prompt_word_count(self):
        text = _ROLES_DIR.joinpath("planner.md").read_text()
        words = len(text.split())
        assert words <= 1500, f"planner.md has {words} words (max 1500)"

    def test_messenger_prompt_word_count(self):
        text = _ROLES_DIR.joinpath("messenger.md").read_text()
        words = len(text.split())
        assert words <= 300, f"messenger.md has {words} words (max 300)"

    def test_reviewer_prompt_word_count(self):
        text = _ROLES_DIR.joinpath("reviewer.md").read_text()
        words = len(text.split())
        assert words <= 450, f"reviewer.md has {words} words (max 450)"


class TestPlannerLanguageRuleDedup:
    """'Answer in {lang' must not be duplicated across planner modules."""

    def test_answer_in_lang_not_duplicated_across_modules(self):
        raw = _ROLES_DIR.joinpath("planner.md").read_text()
        sections = raw.split("<!-- MODULE:")
        mentions = [s for s in sections if "Answer in {lang" in s]
        assert len(mentions) <= 2, (
            f"'Answer in {{lang' appears in {len(mentions)} module sections "
            f"(should be <=2 to avoid duplication)"
        )


class TestBrieferModuleDescriptions:
    """M426: briefer module descriptions must be <=60 chars each."""

    def test_all_descriptions_within_limit(self):
        for name, desc in _BRIEFER_MODULE_DESCRIPTIONS.items():
            assert len(desc) <= 60, (
                f"Module '{name}' description is {len(desc)} chars (max 60): {desc!r}"
            )

    def test_descriptions_are_nonempty(self):
        for name, desc in _BRIEFER_MODULE_DESCRIPTIONS.items():
            assert desc.strip(), f"Module '{name}' has empty description"
