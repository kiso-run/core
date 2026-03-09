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
        assert "usage guide" in self.prompt.lower()
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
        assert "Language handling" in self.prompt


class TestMessengerCriticalRules:
    """Critical messenger rules that must not be removed."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.prompt = (_ROLES_DIR / "messenger.md").read_text()

    def test_voice_rules(self):
        """M277: explicit voice rules."""
        assert "Voice rules" in self.prompt
        assert 'NEVER say "I ran"' in self.prompt

    def test_language_purity(self):
        """M278: no language mixing."""
        assert "Language purity" in self.prompt
        assert "Do not mix languages" in self.prompt

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

    def test_two_categories(self):
        assert '"plan"' in self.prompt
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
        assert "For messenger: modules=[] and skills=[] always" in self.prompt


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

    def test_skill_path(self):
        """M284: skill venv PATH."""
        assert "Skill binaries" in self.prompt


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
