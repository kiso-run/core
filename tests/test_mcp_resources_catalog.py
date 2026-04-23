"""Tests for ``format_mcp_resources`` rendering and briefer integration.

Business requirement: the planner must see MCP resources alongside
MCP methods so it can emit a ``{type: "mcp", method: "__resource_read",
args: {uri: ...}}`` task to read any resource exposed by an MCP
server. The catalog line format mirrors the methods catalog —
``- server:uri — description (mime_type)`` — so the existing
``_filter_briefer_names`` regex treats both as a single namespace.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiso.brain.common import (
    BRIEFER_SCHEMA,
    format_mcp_resources,
    validate_briefing,
)
from kiso.mcp.schemas import MCPResource


def _resource(
    server: str,
    uri: str,
    description: str = "",
    mime_type: str | None = "text/plain",
) -> MCPResource:
    return MCPResource(
        server=server,
        uri=uri,
        name=uri.rsplit("/", 1)[-1] or uri,
        description=description,
        mime_type=mime_type,
    )


@dataclass
class _StubManager:
    catalog: dict[str, list[MCPResource]]

    def available_servers(self) -> list[str]:
        return sorted(self.catalog.keys())

    def list_resources_cached_only(self, name: str) -> list[MCPResource]:
        return self.catalog.get(name, [])


class TestFormat:
    def test_empty_manager_renders_empty(self):
        mgr = _StubManager(catalog={})
        assert format_mcp_resources(mgr) == ""

    def test_none_manager_renders_empty(self):
        assert format_mcp_resources(None) == ""

    def test_single_resource_rendered(self):
        mgr = _StubManager(
            catalog={
                "fs": [_resource("fs", "file:///logs/today.log", "Today log")]
            }
        )
        text = format_mcp_resources(mgr)
        assert "- fs:file:///logs/today.log" in text
        assert "Today log" in text
        assert "(text/plain)" in text

    def test_multiple_servers_sorted(self):
        mgr = _StubManager(
            catalog={
                "b": [_resource("b", "kiso://b/1")],
                "a": [_resource("a", "kiso://a/1")],
            }
        )
        text = format_mcp_resources(mgr)
        lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        assert lines[0].startswith("- a:")
        assert lines[1].startswith("- b:")

    def test_mime_type_absent_not_rendered(self):
        mgr = _StubManager(
            catalog={
                "x": [_resource("x", "kiso://x/1", mime_type=None)]
            }
        )
        text = format_mcp_resources(mgr)
        assert "- x:kiso://x/1" in text
        assert "(" not in text  # no mime parens when absent

    def test_description_empty_still_lists_uri(self):
        mgr = _StubManager(
            catalog={
                "x": [_resource("x", "kiso://x/1", description="")]
            }
        )
        text = format_mcp_resources(mgr)
        assert "- x:kiso://x/1" in text


class TestBrieferSchema:
    def test_schema_has_mcp_resources_field(self):
        schema = BRIEFER_SCHEMA["json_schema"]["schema"]
        props = schema["properties"]
        assert "mcp_resources" in props
        assert props["mcp_resources"]["type"] == "array"
        assert "mcp_resources" in schema["required"]

    def test_validate_rejects_non_list_mcp_resources(self):
        briefing = {
            "modules": [],
            "skills": [],
            "mcp_methods": [],
            "mcp_resources": "not a list",
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        errs = validate_briefing(briefing, check_modules=False)
        assert any("mcp_resources" in e for e in errs)

    def test_validate_accepts_list_mcp_resources(self):
        briefing = {
            "modules": [],
            "skills": [],
            "mcp_methods": [],
            "mcp_resources": ["fs:kiso://x/1"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        errs = validate_briefing(briefing, check_modules=False)
        assert not any("mcp_resources" in e for e in errs)
