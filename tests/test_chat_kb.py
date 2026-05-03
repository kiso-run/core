"""Messenger sanitization + emoji stripping + output-backed learning.

(M1620: the chat_kb fast path / preflight / classifier-driven dispatch
that this file used to exercise is gone. The remaining tests pin
brain-level helpers — sanitizer, emoji stripper, learning filter —
that survive the pipeline cutover unchanged.)
"""

from __future__ import annotations

import pytest

from kiso.brain import (
    EMOJI_STRIP_RE,
    _sanitize_messenger_output,
    clean_learn_items,
    strip_emoji,
)



class TestMessengerSanitization:
    """Messenger output sanitization strips hallucinated wrapper markup."""

    def test_tool_call_block_stripped(self):
        text = 'Hello!\n<tool_call name="search">{"q":"test"}</tool_call>\nHow are you?'
        result = _sanitize_messenger_output(text)
        assert "<tool_call" not in result
        assert "Hello!" in result
        assert "How are you?" in result

    def test_function_call_block_stripped(self):
        text = "Sure!\n<function_call>do_something()</function_call>"
        result = _sanitize_messenger_output(text)
        assert "<function_call" not in result
        assert "Sure!" in result

    def test_orphaned_tags_stripped(self):
        text = "Result: </tool_call> some text"
        result = _sanitize_messenger_output(text)
        assert "</tool_call>" not in result
        assert "some text" in result

    def test_normal_output_unchanged(self):
        text = "Your SSH key is ssh-ed25519 AAAA at ~/.kiso/sys/ssh/id_ed25519.pub"
        result = _sanitize_messenger_output(text)
        assert result == text

    def test_empty_output(self):
        result = _sanitize_messenger_output("")
        assert result == ""

    def test_sanitizer_strips_emoji(self):
        """: messenger sanitizer also strips emoji deterministically."""
        text = "**🎯 Interaction**\n💻 Code\n🌐 Net\n🛠 Wrappers\n🔬 Lab\n📚 Docs"
        result = _sanitize_messenger_output(text)
        for ch in ("🎯", "💻", "🌐", "🛠", "🔬", "📚"):
            assert ch not in result
        # surrounding text preserved
        assert "Interaction" in result
        assert "Code" in result


class TestStripEmoji:
    """: deterministic emoji stripping for messenger output."""

    @pytest.mark.parametrize("emoji,name", [
        ("\U0001F3AF", "1F3AF target"),
        ("\U0001F4BB", "1F4BB laptop"),
        ("\U0001F310", "1F310 globe"),
        ("\U0001F6E0", "1F6E0 hammer-and-wrench"),
        ("\U0001F52C", "1F52C microscope"),
        ("\U0001F4DA", "1F4DA books"),
        ("\u2600", "2600 sun"),
        ("\u27BF", "27BF curly loop"),
        ("\U0001FA9F", "1FA9F window"),
    ])
    def test_strips_each_known_emoji(self, emoji, name):
        text = f"hello {emoji} world"
        assert strip_emoji(text) == "hello  world"

    def test_keeps_alphanumeric_and_punctuation(self):
        text = "Ciao! Sono Kiso, versione 0.8.0."
        assert strip_emoji(text) == text

    def test_keeps_markdown_structure(self):
        text = "**bold** _italic_ `code` # heading\n- bullet"
        assert strip_emoji(text) == text

    def test_handles_empty_and_none_safe(self):
        assert strip_emoji("") == ""

    def test_strips_multiple_in_a_row(self):
        text = "a🎯b💻c"
        assert strip_emoji(text) == "abc"

    def test_regex_parity_with_functional_test(self):
        """The functional test regex must match the production regex."""
        from tests.functional.test_knowledge import _EMOJI_RE
        # Both should match the same character set: probe with all six
        # emoji from the failing run.
        for ch in ("🎯", "💻", "🌐", "🛠", "🔬", "📚"):
            assert _EMOJI_RE.search(ch) is not None
            assert EMOJI_STRIP_RE.search(ch) is not None


class TestOutputBackedLearningIntegration:
    """output-backed learning validation in realistic scenarios."""

    def test_negative_claim_about_present_item_filtered(self):
        """Reviewer says "not stated" but subject IS in output."""
        items = [
            "The kernel release was not stated in the system output",
            "Project uses Flask framework for serving",
        ]
        output = "Linux 6.1.0-20-amd64 #1 SMP kernel release"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1
        assert "Flask" in result[0]

    def test_legitimate_negative_preserved(self):
        """Negative claim where subject truly absent → kept."""
        items = ["Terraform toolchain not available on this host"]
        output = "total 0\nno matching packages found"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1

    def test_combined_filters(self):
        """Short + transient + contradicted all filtered in one pass."""
        items = [
            "too short",                                              # <15 chars
            "nginx installed successfully on the server",             # transient
            "python package not found on the system",                 # contradicted
            "Server runs Ubuntu 22.04 LTS with systemd init system", # valid
        ]
        output = "Python 3.11.2 is installed\npython3 /usr/bin/python3"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1
        assert "Ubuntu" in result[0]
