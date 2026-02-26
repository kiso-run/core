"""Unit tests for kiso/worker/search.py — _parse_search_args and _search_task."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from kiso.brain import SearcherError
from kiso.worker.search import _parse_search_args, _search_task


# ---------------------------------------------------------------------------
# _parse_search_args
# ---------------------------------------------------------------------------


class TestParseSearchArgs:

    # --- None / empty / missing ---

    def test_none_returns_all_none(self):
        assert _parse_search_args(None) == (None, None, None)

    def test_empty_string_returns_all_none(self):
        # Empty string is falsy — treated the same as None
        assert _parse_search_args("") == (None, None, None)

    def test_empty_json_object_returns_all_none(self):
        assert _parse_search_args("{}") == (None, None, None)

    # --- valid args ---

    def test_all_three_params_valid(self):
        args = '{"max_results": 10, "lang": "it", "country": "IT"}'
        assert _parse_search_args(args) == (10, "it", "IT")

    def test_max_results_only(self):
        max_r, lang, country = _parse_search_args('{"max_results": 5}')
        assert max_r == 5
        assert lang is None
        assert country is None

    def test_lang_only(self):
        max_r, lang, country = _parse_search_args('{"lang": "en"}')
        assert max_r is None
        assert lang == "en"
        assert country is None

    def test_extra_fields_are_ignored(self):
        args = '{"max_results": 3, "unknown_key": "ignored", "lang": "fr"}'
        max_r, lang, country = _parse_search_args(args)
        assert max_r == 3
        assert lang == "fr"

    # --- max_results bounds ---

    def test_max_results_zero_clamped_to_one(self):
        max_r, _, _ = _parse_search_args('{"max_results": 0}')
        assert max_r == 1

    def test_max_results_negative_clamped_to_one(self):
        max_r, _, _ = _parse_search_args('{"max_results": -99}')
        assert max_r == 1

    def test_max_results_101_clamped_to_100(self):
        max_r, _, _ = _parse_search_args('{"max_results": 101}')
        assert max_r == 100

    def test_max_results_200_clamped_to_100(self):
        max_r, _, _ = _parse_search_args('{"max_results": 200}')
        assert max_r == 100

    def test_max_results_exactly_1(self):
        max_r, _, _ = _parse_search_args('{"max_results": 1}')
        assert max_r == 1

    def test_max_results_exactly_100(self):
        max_r, _, _ = _parse_search_args('{"max_results": 100}')
        assert max_r == 100

    def test_max_results_float_coerced(self):
        # int(50.5) == 50 — JSON numbers can be floats
        max_r, _, _ = _parse_search_args('{"max_results": 50.5}')
        assert max_r == 50

    # --- type errors ---

    def test_max_results_string_returns_none(self):
        max_r, _, _ = _parse_search_args('{"max_results": "ten"}')
        assert max_r is None

    def test_max_results_list_returns_none(self):
        max_r, _, _ = _parse_search_args('{"max_results": [10]}')
        assert max_r is None

    def test_lang_integer_returns_none(self):
        _, lang, _ = _parse_search_args('{"lang": 42}')
        assert lang is None

    def test_lang_null_returns_none(self):
        # JSON null → Python None → not isinstance(None, str) → None
        _, lang, _ = _parse_search_args('{"lang": null}')
        assert lang is None

    def test_country_list_returns_none(self):
        _, _, country = _parse_search_args('{"country": ["US"]}')
        assert country is None

    def test_country_null_returns_none(self):
        _, _, country = _parse_search_args('{"country": null}')
        assert country is None

    # --- malformed JSON ---

    def test_malformed_json_returns_all_none(self):
        assert _parse_search_args("NOT JSON") == (None, None, None)

    def test_malformed_json_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="kiso.worker.search"):
            _parse_search_args("NOT JSON", task_id=42)
        assert any("malformed" in r.message.lower() for r in caplog.records)

    def test_malformed_json_includes_task_id_in_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="kiso.worker.search"):
            _parse_search_args("{bad}", task_id=99)
        assert any("99" in r.message for r in caplog.records)

    def test_valid_json_no_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="kiso.worker.search"):
            _parse_search_args('{"max_results": 5}')
        assert not caplog.records


# ---------------------------------------------------------------------------
# _search_task
# ---------------------------------------------------------------------------


class TestSearchTask:

    @pytest.fixture
    def mock_config(self, test_config):
        return test_config

    async def test_returns_searcher_result(self, mock_config):
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="results") as m:
            result = await _search_task(mock_config, "python async", None)
        assert result == "results"

    async def test_passes_detail_as_query(self, mock_config):
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="x") as m:
            await _search_task(mock_config, "my query", None)
        assert m.call_args.args[1] == "my query"

    async def test_passes_context(self, mock_config):
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="x") as m:
            await _search_task(mock_config, "q", None, context="previous results")
        assert m.call_args.kwargs["context"] == "previous results"

    async def test_passes_session(self, mock_config):
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="x") as m:
            await _search_task(mock_config, "q", None, session="sess-1")
        assert m.call_args.kwargs["session"] == "sess-1"

    async def test_parsed_args_forwarded(self, mock_config):
        args = '{"max_results": 7, "lang": "de", "country": "DE"}'
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="x") as m:
            await _search_task(mock_config, "q", args)
        kw = m.call_args.kwargs
        assert kw["max_results"] == 7
        assert kw["lang"] == "de"
        assert kw["country"] == "DE"

    async def test_none_args_json_passes_none_params(self, mock_config):
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="x") as m:
            await _search_task(mock_config, "q", None)
        kw = m.call_args.kwargs
        assert kw["max_results"] is None
        assert kw["lang"] is None
        assert kw["country"] is None

    async def test_empty_string_args_json_passes_none_params(self, mock_config):
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="x") as m:
            await _search_task(mock_config, "q", "")
        kw = m.call_args.kwargs
        assert kw["max_results"] is None

    async def test_searcher_error_propagates(self, mock_config):
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock,
                   side_effect=SearcherError("boom")):
            with pytest.raises(SearcherError, match="boom"):
                await _search_task(mock_config, "q", None)

    async def test_task_id_passed_to_parse(self, mock_config, caplog):
        import logging
        with patch("kiso.worker.search.run_searcher", new_callable=AsyncMock, return_value="x"):
            with caplog.at_level(logging.WARNING, logger="kiso.worker.search"):
                await _search_task(mock_config, "q", "{bad json}", task_id=77)
        assert any("77" in r.message for r in caplog.records)
