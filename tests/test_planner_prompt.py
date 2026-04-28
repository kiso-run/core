"""M1579c — planner prompt ask-first + anti-overfitting locks.

The planner prompt today contains two latent problems for the
broker model:

1. **Hardcoded names** — line 91 "(e.g. perplexity / sonar)" — bake
   specific search-engine MCPs into the prompt. The model sees them
   as canonical and falls back to them when the catalog is empty.

2. **No explicit ask-first policy** — when a capability is missing
   the prompt says "propose installing one via needs_install + msg",
   but doesn't tell the planner to use the M1579a `awaits_input`
   field, doesn't forbid guessing URLs, and doesn't forbid pivoting
   to exec.

These static locks pin the post-revision shape:
- `awaits_input` literal present.
- ask-first phrasing visible.
- `perplexity` / `sonar` removed.
- `FORBIDDEN` block present, listing the 3 cited behaviors.
"""

from __future__ import annotations

from pathlib import Path

import pytest


PLANNER_MD = (
    Path(__file__).resolve().parent.parent / "kiso" / "roles" / "planner.md"
)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return PLANNER_MD.read_text()


class TestAskFirstPolicy:
    def test_awaits_input_referenced(self, prompt_text):
        assert "awaits_input" in prompt_text, (
            "planner.md must reference the M1579a `awaits_input` "
            "schema field"
        )

    def test_ask_first_phrasing_present(self, prompt_text):
        """Some variant of 'Do you have a URL ...' must appear so the
        planner has a concrete script to follow when a capability is
        missing."""
        candidates = (
            "Do you have a specific URL",
            "Do you have a URL",
            "Do you have a specific link",
            "do you have a url",
        )
        lower = prompt_text.lower()
        assert any(c.lower() in lower for c in candidates), (
            "planner.md must include an explicit ask-first phrasing "
            "that asks the user for a URL or to search"
        )


class TestAntiOverfitting:
    @pytest.mark.parametrize("forbidden", ["perplexity", "sonar", "tavily"])
    def test_no_hardcoded_search_mcp_names(self, prompt_text, forbidden):
        assert forbidden.lower() not in prompt_text.lower(), (
            f"planner.md must not hardcode {forbidden!r}; use generic "
            f"phrasing like 'any installed search MCP' "
            f"(decision 6, anti-overfitting)"
        )


class TestForbiddenBlock:
    def test_forbidden_header_present(self, prompt_text):
        assert "FORBIDDEN" in prompt_text, (
            "planner.md must include an explicit FORBIDDEN block "
            "listing the three cited broker-model anti-patterns"
        )

    def test_forbidden_block_lists_three_behaviors(self, prompt_text):
        """The block must mention all three failure modes the M1579c
        retrospective identified."""
        lower = prompt_text.lower()
        assert "guessed the url" in lower or "guess the url" in lower, (
            "FORBIDDEN block must call out URL-guessing"
        )
        assert "pivot" in lower, (
            "FORBIDDEN block must call out exec-pivoting on missing MCP"
        )
        assert "search the web" in lower or "high-level" in lower, (
            "FORBIDDEN block must call out high-level intents that "
            "should not become exec"
        )
