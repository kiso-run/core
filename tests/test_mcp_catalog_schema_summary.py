"""Tests for ``format_mcp_catalog`` rendering of input-schema summaries.

Business requirement: the planner's MCP catalog must expose enough
of each method's ``input_schema`` that the LLM can build a valid
call on the first try. A verbose dump would blow the budget; the
catalog shows a **compact** summary — required args with types,
with optional-arg hints and bounded length so the full catalog
stays within the briefer's budget.

Format per method line:
- name-only, no schema → ``- server:method — description``
- name + compact args → ``- server:method(arg:type, opt:type?) — description``
- the summary truncates at 200 chars per line with ``...`` if it
  exceeds the bound.

Required args come first, optional args are marked with ``?``.
Empty / no-properties schema adds no summary. ``type: array`` and
``type: object`` render as ``list`` and ``dict`` to keep the
summary human-readable.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiso.brain.common import format_mcp_catalog
from kiso.mcp.schemas import MCPMethod


def _method(
    server: str,
    name: str,
    description: str = "",
    schema: dict | None = None,
) -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description=description,
        input_schema=schema if schema is not None else {"type": "object"},
        output_schema=None,
        annotations=None,
    )


@dataclass
class _StubManager:
    catalog: dict[str, list[MCPMethod]]

    def available_servers(self) -> list[str]:
        return sorted(self.catalog.keys())

    def list_methods_cached_only(self, name: str) -> list[MCPMethod]:
        return self.catalog.get(name, [])


class TestSchemaSummary:
    def test_required_string_arg_rendered(self):
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        mgr = _StubManager(
            catalog={
                "web": [_method("web", "search", "Search the web", schema=schema)]
            }
        )
        text = format_mcp_catalog(mgr)
        assert "web:search(query:string)" in text

    def test_required_and_optional_args_rendered(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        }
        mgr = _StubManager(
            catalog={
                "web": [_method("web", "search", "Search", schema=schema)]
            }
        )
        text = format_mcp_catalog(mgr)
        line = next(ln for ln in text.splitlines() if "web:search" in ln)
        assert "query:string" in line
        assert "max_results:int?" in line or "max_results:integer?" in line
        # Required comes before optional.
        assert line.index("query:") < line.index("max_results:")

    def test_array_and_object_types_rendered_as_list_and_dict(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array"},
                "meta": {"type": "object"},
            },
            "required": ["tags", "meta"],
        }
        mgr = _StubManager(
            catalog={
                "x": [_method("x", "m", "desc", schema=schema)]
            }
        )
        text = format_mcp_catalog(mgr)
        line = next(ln for ln in text.splitlines() if "x:m" in ln)
        assert "tags:list" in line
        assert "meta:dict" in line

    def test_no_args_method_has_no_parens(self):
        schema = {"type": "object", "properties": {}}
        mgr = _StubManager(
            catalog={
                "x": [_method("x", "ping", "Health check", schema=schema)]
            }
        )
        text = format_mcp_catalog(mgr)
        line = next(ln for ln in text.splitlines() if "x:ping" in ln)
        assert "x:ping(" not in line
        assert "x:ping " in line or line.endswith("x:ping")

    def test_empty_schema_renders_same_as_today(self):
        mgr = _StubManager(
            catalog={
                "x": [_method("x", "m", "desc")]  # default empty {type: object}
            }
        )
        text = format_mcp_catalog(mgr)
        assert "- x:m — desc" in text
        assert "x:m(" not in text

    def test_line_truncated_at_200_chars(self):
        many_props = {f"arg{i}": {"type": "string"} for i in range(80)}
        schema = {
            "type": "object",
            "properties": many_props,
            "required": list(many_props.keys()),
        }
        mgr = _StubManager(
            catalog={
                "x": [_method("x", "many", "many args", schema=schema)]
            }
        )
        text = format_mcp_catalog(mgr)
        line = next(ln for ln in text.splitlines() if "x:many" in ln)
        assert len(line) <= 200
        assert line.endswith("...") or line.endswith("...)")

    def test_method_with_no_description_still_gets_schema(self):
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        mgr = _StubManager(
            catalog={
                "x": [_method("x", "m", description="", schema=schema)]
            }
        )
        text = format_mcp_catalog(mgr)
        assert "x:m(q:string)" in text


class TestBudget:
    """With the schema summary enabled, a 30-method catalog stays
    within the briefer's MCP filter threshold × 200-char bound."""

    def test_thirty_methods_stay_under_budget(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        }
        methods = [
            _method("s", f"m{i}", f"description {i}", schema=schema)
            for i in range(30)
        ]
        mgr = _StubManager(catalog={"s": methods})
        text = format_mcp_catalog(mgr)
        # 30 × 200 chars = 6000 bytes upper bound.
        assert len(text) <= 30 * 200
        assert text.count("s:m") == 30
