"""M1581 — `@pytest.mark.requires_mcp` marker meta-tests.

The marker tells pytest "this test needs MCP `<name>` available in
the catalog". When the test runs, the `mock_mcp_catalog` fixture
auto-populates a default stub for each named MCP so the test author
doesn't have to wire registration manually.

These meta-tests pin the marker → fixture contract.
"""

from __future__ import annotations

import pytest


def test_marker_absent_yields_empty_catalog(mock_mcp_catalog):
    assert mock_mcp_catalog.servers == {}


@pytest.mark.requires_mcp("foo-mcp")
def test_marker_with_single_name_registers_stub(mock_mcp_catalog):
    assert "foo-mcp" in mock_mcp_catalog.servers
    srv = mock_mcp_catalog.servers["foo-mcp"]
    assert "default" in srv.methods


@pytest.mark.requires_mcp(["alpha-mcp", "beta-mcp"])
def test_marker_with_list_registers_each(mock_mcp_catalog):
    assert set(mock_mcp_catalog.servers) == {"alpha-mcp", "beta-mcp"}


@pytest.mark.requires_mcp("foo-mcp")
async def test_default_stub_returns_canonical_string(mock_mcp_catalog):
    mgr = mock_mcp_catalog.build_manager()
    result = await mgr.call_method("foo-mcp", "default", {})
    assert result.is_error is False
    assert result.stdout_text == "[mock response from foo-mcp:default]"


@pytest.mark.requires_mcp("foo-mcp")
def test_register_can_override_marker_default(mock_mcp_catalog):
    """Marker pre-populates a default stub; the test can override it
    by calling `register` again with richer methods."""
    mock_mcp_catalog.register("foo-mcp", {
        "search": lambda **kw: {"hits": [{"url": "https://x"}]},
    })
    srv = mock_mcp_catalog.servers["foo-mcp"]
    assert set(srv.methods) == {"search"}


# ---------------------------------------------------------------------------
# M1613 — capability-flavoured auto-registration
# ---------------------------------------------------------------------------


@pytest.mark.requires_mcp("search-mcp")
def test_search_marker_registers_capability_method(mock_mcp_catalog):
    """``requires_mcp("search-mcp")`` registers a method named
    ``search`` with a description that mentions search/query/web —
    enough for the planner / briefer to recognise the capability
    (M1609 invariant). The legacy generic ``default`` method is no
    longer present for this name.
    """
    srv = mock_mcp_catalog.servers["search-mcp"]
    assert "search" in srv.methods, (
        f"expected `search` method for search-mcp; got {sorted(srv.methods)}"
    )
    desc = srv.descriptions.get("search", "")
    assert "search" in desc.lower() or "query" in desc.lower(), (
        f"description must mention search/query: {desc!r}"
    )


@pytest.mark.requires_mcp("transcriber-mcp")
def test_transcriber_marker_registers_capability_method(mock_mcp_catalog):
    """Same shape as the search test but for transcription: the auto-
    registered method is named ``transcribe`` and the description
    mentions audio/text — generalises the M1613 contract beyond the
    search keyword.
    """
    srv = mock_mcp_catalog.servers["transcriber-mcp"]
    assert "transcribe" in srv.methods, sorted(srv.methods)
    desc = srv.descriptions.get("transcribe", "").lower()
    assert "audio" in desc or "transcrib" in desc or "text" in desc


@pytest.mark.requires_mcp("foo-bar-9000")
def test_unknown_marker_falls_back_to_default(mock_mcp_catalog):
    """Names that don't match any capability keyword fall back to the
    legacy ``default`` method so older tests that register with
    arbitrary names still work unchanged.
    """
    srv = mock_mcp_catalog.servers["foo-bar-9000"]
    assert "default" in srv.methods
    desc = srv.descriptions.get("default", "")
    # Legacy description ("mock method default") is acceptable here.
    assert "mock" in desc.lower() or "default" in desc.lower()
