"""Tests for kiso/brain.py — searcher role functions."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from kiso.brain import (
    SearcherError,
    build_searcher_messages,
    run_searcher,
)
from kiso.config import Config, Provider
from kiso.llm import LLMError


def _make_config(**overrides) -> Config:
    defaults = dict(
        tokens={"cli": "tok"},
        providers={"local": Provider(base_url="http://localhost:11434/v1")},
        users={},
        models={"searcher": "google/gemini-2.5-flash-lite:online"},
        settings={},
        raw={},
    )
    defaults.update(overrides)
    return Config(**defaults)


# --- build_searcher_messages ---


class TestBuildSearcherMessages:
    def test_build_searcher_messages_basic(self):
        """Verify message structure: 2 messages, system + user, query in user content."""
        msgs = build_searcher_messages("best pizza in Rome")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "best pizza in Rome" in msgs[1]["content"]
        assert "## Search Query" in msgs[1]["content"]

    def test_build_searcher_messages_with_params(self):
        """Verify lang/country/max_results are included in ## Search Parameters section."""
        msgs = build_searcher_messages(
            "best SEO agencies",
            lang="it",
            country="IT",
            max_results=10,
        )
        content = msgs[1]["content"]
        assert "## Search Parameters" in content
        assert "lang: it" in content
        assert "country: IT" in content
        assert "max_results: 10" in content

    def test_build_searcher_messages_with_context(self):
        """Verify context section present when context provided."""
        msgs = build_searcher_messages(
            "find restaurants",
            context="User is in Milan and prefers Italian cuisine.",
        )
        content = msgs[1]["content"]
        assert "## Context" in content
        assert "User is in Milan" in content

    def test_build_searcher_messages_no_params(self):
        """Verify no ## Search Parameters section when none provided."""
        msgs = build_searcher_messages("simple query")
        content = msgs[1]["content"]
        assert "## Search Parameters" not in content


# --- run_searcher ---


class TestRunSearcher:
    async def test_run_searcher_success(self):
        """Mock call_llm -> return value passthrough."""
        config = _make_config()
        expected = "Here are the top 5 results for best pizza..."
        with patch("kiso.brain.call_llm", new_callable=AsyncMock, return_value=expected):
            result = await run_searcher(config, "best pizza in Rome")
        assert result == expected

    async def test_run_searcher_error(self):
        """Mock LLMError -> SearcherError raised."""
        config = _make_config()
        with patch(
            "kiso.brain.call_llm",
            new_callable=AsyncMock,
            side_effect=LLMError("API down"),
        ):
            with pytest.raises(SearcherError, match="LLM call failed.*API down"):
                await run_searcher(config, "failing query")
