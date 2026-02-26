"""Search task handler for the kiso worker."""

from __future__ import annotations

import json
import logging

from kiso.brain import SearcherError, run_searcher
from kiso.config import Config

log = logging.getLogger(__name__)


def _parse_search_args(
    args_json: str | None,
    task_id: object = None,
) -> tuple[int | None, str | None, str | None]:
    """Parse and validate search task args JSON. Returns (max_results, lang, country).

    Logs a warning and falls back to defaults on malformed JSON.
    """
    search_params: dict = {}
    if args_json:
        try:
            search_params = json.loads(args_json)
        except json.JSONDecodeError as e:
            log.warning(
                "search task %r: malformed args JSON, using defaults: %s", task_id, e
            )

    max_results = search_params.get("max_results")
    if max_results is not None:
        try:
            max_results = max(1, min(int(max_results), 100))
        except (TypeError, ValueError):
            max_results = None

    lang = search_params.get("lang")
    if not isinstance(lang, str):
        lang = None

    country = search_params.get("country")
    if not isinstance(country, str):
        country = None

    return max_results, lang, country


async def _search_task(
    config: Config,
    detail: str,
    args_json: str | None,
    *,
    context: str = "",
    session: str = "",
    task_id: object = None,
) -> str:
    """Parse search args and run the searcher. Returns the search result text.

    Raises :exc:`~kiso.brain.SearcherError` on failure.
    """
    max_results, lang, country = _parse_search_args(args_json, task_id=task_id)
    return await run_searcher(
        config,
        detail,
        context=context,
        max_results=max_results,
        lang=lang,
        country=country,
        session=session,
    )


__all__ = ["_parse_search_args", "_search_task", "SearcherError"]
