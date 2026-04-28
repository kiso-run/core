"""M1579b — classifier prompt ontology lock tests.

Static checks on `kiso/roles/classifier.md` that pin the chat /
chat_kb / plan boundary post-revision. The classifier today
classifies general-knowledge questions ("what do you know about
flask?") as `chat_kb` whenever the trigger phrase appears, even
when no Known Entities match. The flask test fixture in
`tests/live/test_roles.py` baked this into the live suite.

These locks assert:
- A visible Examples block with the canonical cases.
- The chat_kb definition no longer leads with the
  "(what do you know about X)" parenthetical — that example
  belongs in the conditional Examples block, where its
  with/without-Known-Entities behaviour is explicit.
- The general-knowledge → chat rule remains visible and
  unambiguous.
- No hardcoded MCP / search-engine names anywhere in the
  prompt (decision 6, anti-overfitting).
"""

from __future__ import annotations

from pathlib import Path

import pytest


CLASSIFIER_MD = (
    Path(__file__).resolve().parent.parent / "kiso" / "roles" / "classifier.md"
)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return CLASSIFIER_MD.read_text()


class TestClassifierOntology:
    def test_examples_block_present(self, prompt_text):
        # An explicit "Examples:" or "## Examples" header anchors the
        # block. The actual layout is up to the prompt; the marker is
        # what distinguishes "lots of inline rules" from "an examples
        # section the model can latch onto".
        markers = ("Examples:", "## Examples", "EXAMPLES:")
        assert any(m in prompt_text for m in markers), (
            "classifier.md must include an explicit Examples block "
            "(one of: 'Examples:', '## Examples', 'EXAMPLES:')"
        )

    def test_chat_kb_definition_does_not_lead_with_trigger_phrase(self, prompt_text):
        """The chat_kb bullet must not parenthetically embed the
        'what do you know about X' phrase as its primary marker.
        Doing so causes the model to fire on the trigger alone."""
        for line in prompt_text.splitlines():
            stripped = line.lstrip("- ").strip()
            if stripped.startswith('"chat_kb"'):
                lower = line.lower()
                paren_idx = lower.find("(")
                if paren_idx >= 0:
                    paren_window = lower[paren_idx : paren_idx + 80]
                    assert "what do you know about" not in paren_window, (
                        "chat_kb definition leads with the "
                        "'(what do you know about X)' parenthetical, "
                        "which over-fires on general-knowledge prompts"
                    )
                return
        pytest.fail("could not locate the chat_kb definition line")

    def test_general_knowledge_rule_visible(self, prompt_text):
        """An explicit 'general knowledge → chat' line must remain
        visible, not buried in conditional logic."""
        lower = prompt_text.lower()
        assert "general knowledge" in lower
        for line in prompt_text.splitlines():
            line_l = line.lower()
            if "general knowledge" in line_l and (
                "chat" in line_l or "→ chat" in line_l
            ):
                return
        pytest.fail("general-knowledge → chat rule not on a single line")

    @pytest.mark.parametrize(
        "forbidden",
        [
            "perplexity", "sonar", "tavily", "duckduckgo",
            "search-mcp", "exa-mcp", "kiso-run/",
        ],
    )
    def test_no_hardcoded_mcp_names(self, prompt_text, forbidden):
        assert forbidden.lower() not in prompt_text.lower(), (
            f"classifier.md must not hardcode {forbidden!r} "
            f"(anti-overfitting; decision 6)"
        )

    @pytest.mark.parametrize(
        "phrase",
        [
            "what's my email",
            "what do you know about flask",
            "what is recursion",
            "search for python tutorials",
            "find me an mcp for transcription",
            "why is the disk full",
        ],
    )
    def test_canonical_examples_present(self, prompt_text, phrase):
        """The Examples block must cover the canonical mix: stored
        info, general knowledge, action requests, live system
        queries, capability gaps."""
        assert phrase.lower() in prompt_text.lower(), (
            f"classifier.md must include the canonical example {phrase!r}"
        )
