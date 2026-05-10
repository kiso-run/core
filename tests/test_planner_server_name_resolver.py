"""Unit tests for `_normalize_mcp_server_names`.

When the planner emits `mcp(server=X)` and X is NOT a registered
server, but the task's `method` is unique to one registered
server, the resolver normalizes `task['server']` deterministically.

This closes two known LLM-hallucination patterns:
- "translate" → "translate-mcp" (dropped suffix)
- "playwright" → "browser" (package name vs server name)

The fix is method-driven, not name-driven: the method name is
unique across the catalog (or at worst, has a small candidate
set). When exactly one server registers the method, resolution
is unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiso.brain.planner import _normalize_mcp_server_names


@dataclass
class _Method:
    """Minimal stand-in for the catalog's MCP method record. The
    resolver only consults `name`; downstream `validate_plan`
    (when integration-tested) also reads `input_schema`."""
    name: str
    # Permissive empty schema — accepts any args. Lets the integration
    # test exercise the resolver path without false-positive args
    # validation errors.
    input_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.input_schema is None:
            self.input_schema = {"type": "object", "additionalProperties": True}


def _pool() -> dict[str, list[_Method]]:
    """Catalog stub matching the failure-shape from 2026-05-10:
    `browser` (from @playwright/mcp) exposes `browser_navigate`,
    `translate-mcp` exposes `translate`, etc."""
    return {
        "browser": [
            _Method("browser_navigate"),
            _Method("browser_click"),
            _Method("browser_evaluate"),
        ],
        "translate-mcp": [
            _Method("translate"),
        ],
        "search-mcp": [
            _Method("search"),
        ],
        "ocr": [
            _Method("ocr_image"),
        ],
    }


class TestServerNameResolver:
    def test_translate_resolved_to_translate_mcp(self):
        """The translate-mcp flake: planner emits server='translate'
        instead of 'translate-mcp'. Method 'translate' is unique to
        translate-mcp → normalize."""
        tasks = [
            {"type": "mcp", "server": "translate", "method": "translate",
             "args": {"text": "ciao"}, "detail": "translate text",
             "expect": "translation"},
        ]
        _normalize_mcp_server_names(tasks, _pool())
        assert tasks[0]["server"] == "translate-mcp"

    def test_playwright_resolved_to_browser(self):
        """The browser-mcp flake: planner emits server='playwright'
        (from the @playwright/mcp package name) but the registered
        server is 'browser'. Method 'browser_navigate' is unique to
        the 'browser' server → normalize."""
        tasks = [
            {"type": "mcp", "server": "playwright", "method": "browser_navigate",
             "args": {"url": "https://example.com"}, "detail": "navigate",
             "expect": "page loaded"},
        ]
        _normalize_mcp_server_names(tasks, _pool())
        assert tasks[0]["server"] == "browser"

    def test_already_correct_server_unchanged(self):
        """No mutation when the planner emits the right server."""
        tasks = [
            {"type": "mcp", "server": "browser", "method": "browser_navigate",
             "args": {"url": "https://example.com"}, "detail": "navigate",
             "expect": "page"},
        ]
        _normalize_mcp_server_names(tasks, _pool())
        assert tasks[0]["server"] == "browser"

    def test_unknown_method_leaves_server_alone(self):
        """If the method matches NO registered server, the resolver
        leaves the task as-is (validator will reject downstream with
        a clear error)."""
        tasks = [
            {"type": "mcp", "server": "made-up", "method": "unknown_method",
             "args": {}, "detail": "x", "expect": "y"},
        ]
        _normalize_mcp_server_names(tasks, _pool())
        assert tasks[0]["server"] == "made-up"

    def test_ambiguous_method_leaves_server_alone(self):
        """If the same method name is registered on ≥2 servers, the
        resolver does NOT guess — it leaves the task for the validator
        to reject. Avoids silent wrong-routing on overloaded names."""
        ambiguous_pool = {
            "server-a": [_Method("ping")],
            "server-b": [_Method("ping")],
        }
        tasks = [
            {"type": "mcp", "server": "ping-mcp", "method": "ping",
             "args": {}, "detail": "x", "expect": "y"},
        ]
        _normalize_mcp_server_names(tasks, ambiguous_pool)
        assert tasks[0]["server"] == "ping-mcp"  # unchanged

    def test_synthetic_methods_skipped(self):
        """`__resource_read` and `__prompt_get` are synthetic methods
        every server may expose. Resolution by these would be
        meaningless — the resolver explicitly skips them."""
        tasks = [
            {"type": "mcp", "server": "any-server", "method": "__resource_read",
             "args": {"uri": "file://x"}, "detail": "read", "expect": "ok"},
        ]
        _normalize_mcp_server_names(tasks, _pool())
        assert tasks[0]["server"] == "any-server"  # unchanged

    def test_non_mcp_tasks_skipped(self):
        """exec/msg/replan tasks have no `server` field — resolver
        must not touch them."""
        tasks = [
            {"type": "exec", "server": "irrelevant",
             "detail": "ls", "expect": "files"},
            {"type": "msg", "detail": "say hi", "expect": None},
        ]
        _normalize_mcp_server_names(tasks, _pool())
        assert tasks[0].get("server") == "irrelevant"
        assert tasks[1].get("server") is None

    def test_none_pool_is_noop(self):
        """When mcp_methods_pool is None (no catalog provided), the
        resolver short-circuits — nothing to resolve against."""
        tasks = [
            {"type": "mcp", "server": "translate", "method": "translate",
             "args": {}, "detail": "x", "expect": "y"},
        ]
        _normalize_mcp_server_names(tasks, None)
        assert tasks[0]["server"] == "translate"

    def test_empty_pool_is_noop(self):
        """Empty pool → no candidates → no mutation."""
        tasks = [
            {"type": "mcp", "server": "translate", "method": "translate",
             "args": {}, "detail": "x", "expect": "y"},
        ]
        _normalize_mcp_server_names(tasks, {})
        assert tasks[0]["server"] == "translate"

    def test_multiple_tasks_resolved_independently(self):
        """Multi-task plan: each mcp task resolves independently."""
        tasks = [
            {"type": "mcp", "server": "playwright", "method": "browser_navigate",
             "args": {"url": "https://example.com"}, "detail": "navigate",
             "expect": "page"},
            {"type": "exec", "detail": "ls", "expect": "files"},
            {"type": "mcp", "server": "translate", "method": "translate",
             "args": {"text": "ciao"}, "detail": "translate",
             "expect": "english"},
            {"type": "msg", "detail": "report", "expect": None},
        ]
        _normalize_mcp_server_names(tasks, _pool())
        assert tasks[0]["server"] == "browser"
        assert tasks[2]["server"] == "translate-mcp"


class TestServerNameResolverIntegratedWithValidate:
    """End-to-end: validate_plan resolves names before validating, so
    a server-name-confused plan that the resolver can fix passes
    validation. Closes the planner.md regression where 'playwright'
    or 'translate' would surface as 'server is not available' and
    cause a curl fallback."""

    def test_validate_plan_passes_after_resolution(self):
        from kiso.brain.planner import validate_plan

        plan = {
            "goal": "Navigate to a URL via the browser MCP and report the title",
            "tasks": [
                {"type": "mcp", "server": "playwright", "method": "browser_navigate",
                 "args": {"url": "https://example.com"},
                 "detail": "navigate to example.com",
                 "expect": "page loaded"},
                {"type": "msg", "detail": "Report the page title.", "expect": None},
            ],
        }
        errors = validate_plan(plan, mcp_methods_pool=_pool())
        # Server name was 'playwright' (hallucination); resolver
        # normalized to 'browser'; validation now passes.
        assert errors == []
        assert plan["tasks"][0]["server"] == "browser"

    def test_validate_plan_still_rejects_truly_unknown_server(self):
        """If neither name nor method match anything, the validator
        still emits the original 'server is not available' error —
        the resolver only fixes ambiguity when method resolves it."""
        from kiso.brain.planner import validate_plan

        plan = {
            "goal": "do something",
            "tasks": [
                {"type": "mcp", "server": "fictional", "method": "fictional_op",
                 "args": {}, "detail": "x", "expect": "y"},
                {"type": "msg", "detail": "report", "expect": None},
            ],
        }
        errors = validate_plan(plan, mcp_methods_pool=_pool())
        assert any("not available" in e for e in errors)
