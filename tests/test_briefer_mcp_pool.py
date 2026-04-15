"""Tests for M1370 — MCP methods in the briefer context pool.

Architectural gap closed by this milestone: today the briefer
receives `wrappers`, `recipes`, `modules`, `entities`, `tags`,
`system_env` in its `context_pool`, but **not** `mcp_methods`.
The briefer's output schema (`BRIEFER_SCHEMA`) already declares
`mcp_methods` as a selectable category, but the briefer never
saw the catalog, so it could only return MCP method selections
by accident or hallucination.

After M1370:

- `format_mcp_catalog(manager)` produces a flat string
  (`server:method — description` lines) suitable for the briefer
  prompt, using only methods the manager has already cached
  (no spawning at briefer time).
- `_CONTEXT_POOL_SECTIONS` includes `("mcp_methods", "Available
  MCP Methods")`, so `build_briefer_messages` renders the section
  whenever the planner places text in `context_pool["mcp_methods"]`.
- `build_planner_messages` accepts an optional
  `mcp_catalog_text` parameter and stores it in the pool.
- The post-validation filter (`run_briefer`) intersects the
  briefer's returned `mcp_methods` against the same `mcp_methods`
  pool key shown in the prompt — not against a separate
  `mcp_methods_pool` legacy key.
- `kiso/roles/briefer.md` describes the input section so the
  briefer LLM knows it can select from it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from kiso.brain.common import (
    _CONTEXT_POOL_SECTIONS,
    _filter_briefer_names,
    build_briefer_messages,
    format_mcp_catalog,
)
from kiso.mcp.schemas import MCPMethod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _method(server: str, name: str, description: str = "") -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description=description,
        input_schema={"type": "object"},
        output_schema=None,
        annotations=None,
    )


@dataclass
class _StubManager:
    """Minimal MCPManager-shaped stub for unit tests.

    Provides the two methods `format_mcp_catalog` actually uses:
    `available_servers()` and `list_methods_cached_only(name)`.
    The latter must NOT spawn — it returns whatever is in the cache,
    or an empty list if the server has never been queried.
    """

    catalog: dict[str, list[MCPMethod]]

    def available_servers(self) -> list[str]:
        return sorted(self.catalog.keys())

    def list_methods_cached_only(self, name: str) -> list[MCPMethod]:
        return self.catalog.get(name, [])


# ---------------------------------------------------------------------------
# format_mcp_catalog
# ---------------------------------------------------------------------------


class TestFormatMcpCatalog:
    """The catalog formatter renders cached methods only (no spawning)."""

    def test_empty_manager_returns_empty_string(self) -> None:
        assert format_mcp_catalog(_StubManager(catalog={})) == ""

    def test_single_server_one_method(self) -> None:
        manager = _StubManager(
            catalog={
                "filesystem": [
                    _method("filesystem", "read_file", "Read a file from disk"),
                ]
            }
        )
        text = format_mcp_catalog(manager)
        assert "filesystem:read_file" in text
        assert "Read a file from disk" in text

    def test_multiple_servers_alphabetical(self) -> None:
        manager = _StubManager(
            catalog={
                "github": [_method("github", "create_issue", "Create an issue")],
                "filesystem": [_method("filesystem", "read_file", "Read a file")],
            }
        )
        text = format_mcp_catalog(manager)
        # Sorted: filesystem before github
        assert text.index("filesystem:read_file") < text.index("github:create_issue")

    def test_method_line_format(self) -> None:
        """Each method renders as `- server:method — description`."""
        manager = _StubManager(
            catalog={
                "github": [_method("github", "create_issue", "Open a new issue")]
            }
        )
        text = format_mcp_catalog(manager)
        # Must use the bullet-then-name format (- name) so the existing
        # _filter_briefer_names parser (regex ^-\s+(\S+)) can extract
        # qualified names from the rendered text.
        assert any(
            line.startswith("- github:create_issue") for line in text.splitlines()
        )

    def test_uncached_server_yields_no_lines(self) -> None:
        """If the server has no cached methods, it does not appear at all."""
        manager = _StubManager(
            catalog={
                "filesystem": [_method("filesystem", "read_file", "Read")],
                "uncached_server": [],
            }
        )
        text = format_mcp_catalog(manager)
        assert "filesystem:read_file" in text
        assert "uncached_server" not in text


# ---------------------------------------------------------------------------
# Briefer prompt section integration
# ---------------------------------------------------------------------------


class TestContextPoolSectionWiring:
    """`_CONTEXT_POOL_SECTIONS` and `build_briefer_messages` wire mcp_methods."""

    def test_mcp_methods_in_context_pool_sections(self) -> None:
        keys = [k for k, _ in _CONTEXT_POOL_SECTIONS]
        assert "mcp_methods" in keys, (
            "_CONTEXT_POOL_SECTIONS must include `mcp_methods` so "
            "build_briefer_messages renders the Available MCP Methods "
            "section when context_pool[mcp_methods] is set."
        )

    def test_mcp_methods_section_heading(self) -> None:
        for key, heading in _CONTEXT_POOL_SECTIONS:
            if key == "mcp_methods":
                assert heading == "Available MCP Methods"
                return
        pytest.fail("mcp_methods section not found")

    def test_briefer_messages_render_mcp_methods_section(self) -> None:
        catalog_text = (
            "- filesystem:read_file — Read a file\n"
            "- github:create_issue — Open a new issue"
        )
        messages = build_briefer_messages(
            "planner",
            "use a tool to write something",
            {"mcp_methods": catalog_text},
        )
        # The user message should contain the section
        user_content = messages[-1]["content"]
        assert "## Available MCP Methods" in user_content
        assert "filesystem:read_file" in user_content
        assert "github:create_issue" in user_content

    def test_briefer_messages_omit_section_when_empty(self) -> None:
        messages = build_briefer_messages(
            "planner",
            "do something",
            {"mcp_methods": ""},
        )
        user_content = messages[-1]["content"]
        assert "Available MCP Methods" not in user_content


# ---------------------------------------------------------------------------
# Post-filter consistency
# ---------------------------------------------------------------------------


class TestPostFilterReadsSamePool:
    """`_filter_briefer_names` parses the same text the briefer saw.

    `run_briefer` post-filters hallucinated mcp_method names against a
    pool. After M1370 that pool is the same string set into
    `context_pool[mcp_methods]` for the prompt — not a separate
    `mcp_methods_pool` legacy key. Verified end-to-end via the
    parser used by both sides.
    """

    def test_filter_extracts_qualified_names_from_catalog_text(self) -> None:
        catalog_text = (
            "- filesystem:read_file — Read a file\n"
            "- github:create_issue — Open a new issue"
        )
        # An LLM might return the qualified names + one hallucinated one.
        filtered = _filter_briefer_names(
            ["filesystem:read_file", "github:create_issue", "fake:hallucinated"],
            catalog_text,
            "mcp method",
        )
        assert "filesystem:read_file" in filtered
        assert "github:create_issue" in filtered
        assert "fake:hallucinated" not in filtered

    def test_filter_returns_empty_when_pool_empty(self) -> None:
        filtered = _filter_briefer_names(
            ["filesystem:read_file"],
            "",
            "mcp method",
        )
        assert filtered == []


# ---------------------------------------------------------------------------
# Planner-side integration
# ---------------------------------------------------------------------------


class TestBuildPlannerMessagesAcceptsCatalog:
    """`build_planner_messages` takes an optional catalog text and uses it."""

    @pytest.mark.asyncio
    async def test_signature_accepts_mcp_catalog_text(self) -> None:
        """The function's signature must include `mcp_catalog_text`.

        Cheap structural test — full integration verified separately
        because build_planner_messages requires a real db connection.
        """
        import inspect

        from kiso.brain.planner import build_planner_messages

        sig = inspect.signature(build_planner_messages)
        assert "mcp_catalog_text" in sig.parameters, (
            "build_planner_messages must accept an `mcp_catalog_text` "
            "parameter so callers can inject a pre-formatted MCP catalog "
            "into the briefer's context pool."
        )
        # Default is None — backward compatible with callers that don't
        # have an MCPManager in scope yet.
        assert sig.parameters["mcp_catalog_text"].default is None


# ---------------------------------------------------------------------------
# briefer.md rules
# ---------------------------------------------------------------------------


class TestBrieferRoleFile:
    """`briefer.md` must describe the new input section."""

    def test_briefer_md_mentions_available_mcp_methods_section(self) -> None:
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "kiso"
            / "roles"
            / "briefer.md"
        ).read_text()
        assert "Available MCP Methods" in text, (
            "briefer.md must describe the 'Available MCP Methods' input "
            "section so the briefer LLM knows it can select from it."
        )
