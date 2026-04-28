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
