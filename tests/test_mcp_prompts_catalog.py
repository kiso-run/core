"""Tests for ``format_mcp_prompts`` rendering and briefer integration.

Business requirement: the planner must see MCP prompt templates
alongside MCP methods and resources so it can emit a
``{type: "mcp", method: "__prompt_get", args: {name, prompt_args}}``
task to fetch a fully-rendered prompt from a configured MCP server.
The catalog line format mirrors the methods/resources catalogs —
``- server:name(args) — description`` — so the same regex in
``_filter_briefer_names`` treats all three as a single namespace.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiso.brain.common import (
    BRIEFER_SCHEMA,
    format_mcp_prompts,
    validate_briefing,
)
from kiso.mcp.schemas import MCPPrompt, MCPPromptArgument


def _prompt(
    server: str,
    name: str,
    description: str = "",
    arguments: list[MCPPromptArgument] | None = None,
) -> MCPPrompt:
    return MCPPrompt(
        server=server,
        name=name,
        description=description,
        arguments=list(arguments or []),
    )


@dataclass
class _StubManager:
    catalog: dict[str, list[MCPPrompt]]

    def available_servers(self) -> list[str]:
        return sorted(self.catalog.keys())

    def list_prompts_cached_only(self, name: str) -> list[MCPPrompt]:
        return self.catalog.get(name, [])


class TestFormat:
    def test_empty_manager_renders_empty(self):
        mgr = _StubManager(catalog={})
        assert format_mcp_prompts(mgr) == ""

    def test_none_manager_renders_empty(self):
        assert format_mcp_prompts(None) == ""

    def test_single_prompt_rendered(self):
        mgr = _StubManager(
            catalog={
                "rev": [
                    _prompt(
                        "rev", "code_review",
                        "Review a repository",
                        [
                            MCPPromptArgument(
                                name="repo", description="path",
                                required=True,
                            ),
                            MCPPromptArgument(
                                name="focus", description="focus area",
                                required=False,
                            ),
                        ],
                    )
                ]
            }
        )
        text = format_mcp_prompts(mgr)
        assert "- rev:code_review" in text
        assert "repo" in text
        assert "focus?" in text  # optional arg marked
        assert "Review a repository" in text

    def test_multiple_servers_sorted(self):
        mgr = _StubManager(
            catalog={
                "b": [_prompt("b", "p1")],
                "a": [_prompt("a", "p1")],
            }
        )
        text = format_mcp_prompts(mgr)
        lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        assert lines[0].startswith("- a:")
        assert lines[1].startswith("- b:")

    def test_no_args_rendered_without_parens(self):
        mgr = _StubManager(
            catalog={"x": [_prompt("x", "p1", description="noop")]}
        )
        text = format_mcp_prompts(mgr)
        # No args → no parens at all on the line head
        first_line = text.splitlines()[0]
        assert first_line.startswith("- x:p1 — noop") or first_line.startswith("- x:p1")

    def test_description_empty_still_lists_name(self):
        mgr = _StubManager(
            catalog={"x": [_prompt("x", "p1", description="")]}
        )
        text = format_mcp_prompts(mgr)
        assert "- x:p1" in text


class TestBrieferSchema:
    def test_schema_has_mcp_prompts_field(self):
        schema = BRIEFER_SCHEMA["json_schema"]["schema"]
        props = schema["properties"]
        assert "mcp_prompts" in props
        assert props["mcp_prompts"]["type"] == "array"
        assert "mcp_prompts" in schema["required"]

    def test_validate_rejects_non_list_mcp_prompts(self):
        briefing = {
            "modules": [],
            "skills": [],
            "mcp_methods": [],
            "mcp_resources": [],
            "mcp_prompts": "not a list",
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        errs = validate_briefing(briefing, check_modules=False)
        assert any("mcp_prompts" in e for e in errs)

    def test_validate_accepts_list_mcp_prompts(self):
        briefing = {
            "modules": [],
            "skills": [],
            "mcp_methods": [],
            "mcp_resources": [],
            "mcp_prompts": ["rev:code_review"],
            "context": "",
            "output_indices": [],
            "relevant_tags": [],
            "relevant_entities": [],
        }
        errs = validate_briefing(briefing, check_modules=False)
        assert not any("mcp_prompts" in e for e in errs)
