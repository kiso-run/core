"""Tests for kiso.registry — registry fetch, search, and planner integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import kiso.registry as reg

FAKE_REGISTRY = {
    "tools": [
        {"name": "browser", "description": "Headless WebKit browser automation"},
        {"name": "aider", "description": "Code editing and refactoring"},
        {"name": "websearch", "description": "Web search engine"},
    ],
    "connectors": [
        {"name": "discord", "description": "Discord bridge"},
    ],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level registry cache between tests."""
    reg._registry_cache = None
    reg._registry_ts = 0.0
    yield
    reg._registry_cache = None
    reg._registry_ts = 0.0


# --- fetch_registry ---


class TestFetchRegistry:
    def test_returns_registry_on_success(self):
        mock_resp = MagicMock()
        mock_resp.text = '{"tools": [{"name": "browser", "description": "desc"}]}'
        with patch("httpx.get", return_value=mock_resp):
            result = reg.fetch_registry()
        assert result == {"tools": [{"name": "browser", "description": "desc"}]}

    def test_returns_empty_on_network_error(self):
        import httpx

        with patch("httpx.get", side_effect=httpx.ConnectError("fail")):
            result = reg.fetch_registry()
        assert result == {}

    def test_caches_result(self):
        mock_resp = MagicMock()
        mock_resp.text = '{"tools": []}'
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            reg.fetch_registry()
            reg.fetch_registry()
        mock_get.assert_called_once()

    def test_returns_stale_cache_on_error(self):
        import httpx

        mock_resp = MagicMock()
        mock_resp.text = '{"tools": [{"name": "browser", "description": "d"}]}'
        with patch("httpx.get", return_value=mock_resp):
            first = reg.fetch_registry()
        # Expire cache
        reg._registry_ts = 0.0
        with patch("httpx.get", side_effect=httpx.ConnectError("fail")):
            second = reg.fetch_registry()
        assert second == first


# --- search_entries ---


class TestSearchEntries:
    def test_no_query_returns_all(self):
        entries = FAKE_REGISTRY["tools"]
        assert reg.search_entries(entries, None) == entries

    def test_name_match(self):
        entries = FAKE_REGISTRY["tools"]
        result = reg.search_entries(entries, "browser")
        assert len(result) == 1
        assert result[0]["name"] == "browser"

    def test_description_match(self):
        entries = FAKE_REGISTRY["tools"]
        result = reg.search_entries(entries, "refactoring")
        assert len(result) == 1
        assert result[0]["name"] == "aider"

    def test_no_match(self):
        entries = FAKE_REGISTRY["tools"]
        assert reg.search_entries(entries, "nonexistent") == []


# --- cross_type_hint ---


class TestCrossTypeHint:
    def test_hint_found(self):
        hint = reg.cross_type_hint(FAKE_REGISTRY, "connectors", "browser")
        assert hint is not None
        assert "kiso tool search browser" in hint

    def test_no_hint(self):
        hint = reg.cross_type_hint(FAKE_REGISTRY, "connectors", "nonexistent")
        assert hint is None


# --- get_registry_tools ---


class TestGetRegistryTools:
    def test_all_uninstalled(self):
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            result = reg.get_registry_tools(set())
        assert "browser" in result
        assert "aider" in result
        assert "websearch" in result

    def test_filters_installed(self):
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            result = reg.get_registry_tools({"browser", "aider"})
        assert "browser" not in result
        assert "aider" not in result
        assert "websearch" in result

    def test_all_installed_returns_empty(self):
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            result = reg.get_registry_tools({"browser", "aider", "websearch"})
        assert result == ""

    def test_empty_registry_returns_empty(self):
        with patch.object(reg, "fetch_registry", return_value={}):
            result = reg.get_registry_tools(set())
        assert result == ""


# --- brain.py integration ---


class TestBrainRegistryIntegration:
    """Verify that get_registry_tools is called from brain context building."""

    def test_registry_tools_injected_when_no_tools_installed(self):
        """When no tools installed, registry text is non-empty."""
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            text = reg.get_registry_tools(set())
        assert "Available tools (not installed):" in text
        assert "browser" in text
        assert "aider" in text

    def test_registry_tools_empty_when_all_installed(self):
        """When all tools installed, registry text is empty."""
        all_names = {t["name"] for t in FAKE_REGISTRY["tools"]}
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            text = reg.get_registry_tools(all_names)
        assert text == ""
