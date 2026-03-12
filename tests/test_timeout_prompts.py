"""M427 — Integration tests for timeout unification and prompt sizes."""

from pathlib import Path

import pytest

from kiso.brain import _BRIEFER_MODULE_DESCRIPTIONS, _load_modular_prompt


# ---------------------------------------------------------------------------
# 1. Timeout tests
# ---------------------------------------------------------------------------


class TestTimeoutUnification:
    """M422: per-role timeouts removed, single llm_timeout used."""

    def test_default_config_has_no_per_role_timeouts(self):
        from kiso.config import SETTINGS_DEFAULTS
        assert "planner_timeout" not in SETTINGS_DEFAULTS
        assert "messenger_timeout" not in SETTINGS_DEFAULTS
        assert "llm_timeout" in SETTINGS_DEFAULTS



# ---------------------------------------------------------------------------
# 2. Prompt size regression tests
# ---------------------------------------------------------------------------

ROLES_DIR = Path(__file__).parent.parent / "kiso" / "roles"


class TestPromptSizes:
    """Prompt token counts must stay within budget after M423-M425 optimization."""

    def test_planner_prompt_word_count(self):
        text = ROLES_DIR.joinpath("planner.md").read_text()
        words = len(text.split())
        assert words <= 1200, f"planner.md has {words} words (max 1200)"

    def test_messenger_prompt_word_count(self):
        text = ROLES_DIR.joinpath("messenger.md").read_text()
        words = len(text.split())
        assert words <= 300, f"messenger.md has {words} words (max 300)"

    def test_reviewer_prompt_word_count(self):
        text = ROLES_DIR.joinpath("reviewer.md").read_text()
        words = len(text.split())
        assert words <= 450, f"reviewer.md has {words} words (max 450)"


class TestPlannerLanguageRuleDedup:
    """'Answer in {lang' must not be duplicated across planner modules."""

    def test_answer_in_lang_not_duplicated_across_modules(self):
        raw = ROLES_DIR.joinpath("planner.md").read_text()
        # Split by MODULE markers to get sections
        sections = raw.split("<!-- MODULE:")
        # Count how many sections mention "Answer in {lang"
        mentions = [s for s in sections if "Answer in {lang" in s]
        assert len(mentions) <= 2, (
            f"'Answer in {{lang' appears in {len(mentions)} module sections "
            f"(should be ≤2 to avoid duplication)"
        )


class TestBrieferModuleDescriptions:
    """M426: briefer module descriptions must be ≤60 chars each."""

    def test_all_descriptions_within_limit(self):
        for name, desc in _BRIEFER_MODULE_DESCRIPTIONS.items():
            assert len(desc) <= 60, (
                f"Module '{name}' description is {len(desc)} chars (max 60): {desc!r}"
            )

    def test_descriptions_are_nonempty(self):
        for name, desc in _BRIEFER_MODULE_DESCRIPTIONS.items():
            assert desc.strip(), f"Module '{name}' has empty description"
