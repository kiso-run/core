"""Unit tests for the M1647 deterministic capability-match augmenter.

The briefer is an LLM and sometimes drops MCP methods that match a
user-named capability ("OCR", "search", "navigate", ...) despite the
prompt rule asking it not to. The augmenter is the deterministic
safety net: after the briefer returns, we scan the user's task
description for tokens that match any catalog method's name or
description, and ensure those methods appear in `mcp_methods`. This
mechanism is generalist (no per-test or per-capability hardcoding)
and deterministic (no LLM judgment).

The pool format matches `format_mcp_catalog`:
    - server:method(args) — description
"""

from __future__ import annotations

import pytest

from kiso.brain.common import _augment_capability_matches


_POOL = (
    "- ocr-mcp:extract_text() — Extract text from an image via OCR.\n"
    "- browser-mcp:navigate(url) — Navigate to a URL and return page content.\n"
    "- search-mcp:search(query) — Search the web and return ranked results.\n"
    "- transcriber-mcp:transcribe(audio) — Transcribe an audio file to text.\n"
    "- translate-mcp:translate(text) — Translate text from one language to another.\n"
)


class TestCapabilityAugmenter:
    """`_augment_capability_matches` must add catalog methods whose
    name/description tokens overlap user-message tokens, while leaving
    user-irrelevant methods out."""

    def test_acronym_in_description_triggers_match(self):
        """User says 'OCR' — method description contains 'OCR' (uppercase
        acronym). Must include even though method name is 'extract_text'
        (no 'ocr' in name)."""
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="ora estrai il testo (OCR) dal contenuto",
            pool_text=_POOL,
        )
        assert "ocr-mcp:extract_text" in result

    def test_method_name_token_match(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="naviga a https://example.com",
            pool_text=_POOL,
        )
        # Italian "naviga" doesn't match "navigate" — we expect token
        # match in the method NAME or DESCRIPTION, not via translation.
        # English equivalent must work though:
        result2 = _augment_capability_matches(
            existing,
            task_description="please navigate to https://example.com",
            pool_text=_POOL,
        )
        assert "browser-mcp:navigate" in result2

    def test_no_user_match_returns_unchanged(self):
        existing: list[str] = ["ocr-mcp:extract_text"]
        result = _augment_capability_matches(
            existing,
            task_description="ciao come va?",
            pool_text=_POOL,
        )
        # No capability tokens in greeting → no augmentation; existing
        # entries are preserved unchanged.
        assert result == existing

    def test_dedup_preserves_existing(self):
        """If briefer already included the right method, augmenter
        must not duplicate."""
        existing = ["ocr-mcp:extract_text"]
        result = _augment_capability_matches(
            existing,
            task_description="please run OCR on the image",
            pool_text=_POOL,
        )
        # exactly one entry, no duplicate
        assert result.count("ocr-mcp:extract_text") == 1

    def test_empty_pool_is_safe(self):
        """No catalog → augmenter is a no-op."""
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="run OCR on it",
            pool_text="",
        )
        assert result == []

    def test_multiple_matches_all_included(self):
        """User asks to navigate AND OCR in one message — both methods
        must be surfaced."""
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="navigate to a URL then OCR the result",
            pool_text=_POOL,
        )
        assert "browser-mcp:navigate" in result
        assert "ocr-mcp:extract_text" in result

    def test_short_token_filter_excludes_noise(self):
        """User stopwords ('the', 'and', 'for', 'to') must not
        trigger spurious matches even if those tokens appear in
        descriptions."""
        existing: list[str] = []
        # 'and' appears in many descriptions but the augmenter must not
        # match on stopwords.
        result = _augment_capability_matches(
            existing,
            task_description="the and for or",
            pool_text=_POOL,
        )
        assert result == []

    def test_search_token_matches_search_method(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="search the web for python tutorials",
            pool_text=_POOL,
        )
        assert "search-mcp:search" in result

    def test_transcribe_token_matches(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="please transcribe this audio file",
            pool_text=_POOL,
        )
        assert "transcriber-mcp:transcribe" in result


class TestMultilingualCapabilityMatch:
    """The augmenter must surface the right MCP regardless of the
    user's UI language. The catalog stays English; the augmenter
    normalizes user tokens via a synonym map."""

    def test_italian_traduci_matches_translate(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="ora traduci in inglese il contenuto della pagina",
            pool_text=_POOL,
        )
        assert "translate-mcp:translate" in result, (
            f"Italian 'traduci' must surface translate-mcp via the "
            f"multilingual synonym map. Got {result}."
        )

    def test_italian_cerca_matches_search(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="cerca documentazione su python",
            pool_text=_POOL,
        )
        assert "search-mcp:search" in result

    def test_italian_naviga_matches_navigate(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="naviga a https://example.com",
            pool_text=_POOL,
        )
        assert "browser-mcp:navigate" in result

    def test_italian_trascrivi_matches_transcribe(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="trascrivi questo audio",
            pool_text=_POOL,
        )
        assert "transcriber-mcp:transcribe" in result

    def test_spanish_traducir_matches_translate(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="traducir esto al inglés por favor",
            pool_text=_POOL,
        )
        assert "translate-mcp:translate" in result

    def test_french_traduire_matches_translate(self):
        existing: list[str] = []
        result = _augment_capability_matches(
            existing,
            task_description="traduire ceci en anglais s'il vous plaît",
            pool_text=_POOL,
        )
        assert "translate-mcp:translate" in result
