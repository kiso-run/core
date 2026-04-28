"""M1580 — Mock MCP framework meta-tests.

The framework lets test authors register fake MCP servers with
arbitrary names and method callbacks, then build an `MCPManager`
wired to those mocks. Goals:

- Generalist: any name, any method signature; no hardcoded
  search-mcp / transcriber-mcp / etc. shapes.
- Catalog-level: methods are visible through the same
  `MCPManager.list_methods` path the briefer uses, so end-to-end
  flows exercise the real planner→briefer→worker dispatch shape.
- Cheap: in-process by default (composes the existing FakeClient
  pattern from `tests/test_mcp_manager.py`).

These meta-tests pin the fixture's contract.
"""

from __future__ import annotations

import pytest

from tests._mcp_mock import MockMCPCatalog


class TestMockMCPFramework:
    async def test_register_and_dispatch(self, mock_mcp_catalog):
        captured: dict = {}

        def bar_cb(**kwargs):
            captured["args"] = kwargs
            return {"ok": True}

        mock_mcp_catalog.register("foo-mcp", {"bar": bar_cb})
        mgr = mock_mcp_catalog.build_manager()

        methods = await mgr.list_methods("foo-mcp")
        assert {m.name for m in methods} == {"bar"}

        result = await mgr.call_method("foo-mcp", "bar", {"q": "test"})
        assert result.is_error is False
        assert captured == {"args": {"q": "test"}}

    async def test_assert_called_helper(self, mock_mcp_catalog):
        mock_mcp_catalog.register("foo-mcp", {"bar": lambda **kw: {"ok": True}})
        mgr = mock_mcp_catalog.build_manager()
        await mgr.call_method("foo-mcp", "bar", {"x": 1})
        mock_mcp_catalog.assert_called("foo-mcp", "bar", args={"x": 1})

    async def test_assert_called_raises_when_not_called(self, mock_mcp_catalog):
        mock_mcp_catalog.register("foo-mcp", {"bar": lambda **kw: {"ok": True}})
        mock_mcp_catalog.build_manager()
        with pytest.raises(AssertionError):
            mock_mcp_catalog.assert_called("foo-mcp", "bar")

    async def test_multiple_servers(self, mock_mcp_catalog):
        mock_mcp_catalog.register(
            "foo-mcp", {"bar": lambda **kw: {"src": "foo"}},
        )
        mock_mcp_catalog.register(
            "baz-mcp", {"qux": lambda **kw: {"src": "baz"}},
        )
        mgr = mock_mcp_catalog.build_manager()

        await mgr.call_method("foo-mcp", "bar", {})
        await mgr.call_method("baz-mcp", "qux", {})

        mock_mcp_catalog.assert_called("foo-mcp", "bar")
        mock_mcp_catalog.assert_called("baz-mcp", "qux")

    async def test_each_test_gets_clean_catalog(self, mock_mcp_catalog):
        """The fixture is function-scoped — registrations from prior
        tests must not leak in."""
        assert mock_mcp_catalog.servers == {}

    async def test_callback_return_dict_becomes_structured_content(
        self, mock_mcp_catalog,
    ):
        mock_mcp_catalog.register("foo", {"hello": lambda **kw: {"reply": "hi"}})
        mgr = mock_mcp_catalog.build_manager()
        result = await mgr.call_method("foo", "hello", {})
        assert result.structured_content == {"reply": "hi"}
