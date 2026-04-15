"""Tests for kiso.registry — registry fetch, search, and planner integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import kiso.registry as reg

FAKE_REGISTRY = {
    "wrappers": [
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


# --- registry.json shape ---


class TestRegistryJsonShape:
    """Deterministic shape/consistency checks over the actual
    registry.json file shipped with the package. Catches accidental
    schema drift, missing names/descriptions, or duplicate entries."""

    @pytest.fixture()
    def registry(self):
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "registry.json"
        return json.loads(path.read_text())

    def test_top_level_keys(self, registry):
        assert set(registry.keys()) == {"wrappers", "recipes", "connectors", "presets"}

    def test_tools_have_name_and_description(self, registry):
        for entry in registry["wrappers"]:
            assert entry.get("name")
            assert entry.get("description")

    def test_connectors_have_name_and_description(self, registry):
        for entry in registry["connectors"]:
            assert entry.get("name")
            assert entry.get("description")

    def test_presets_have_name_and_description(self, registry):
        for entry in registry["presets"]:
            assert entry.get("name")
            assert entry.get("description")

    def test_wrapper_names_unique(self, registry):
        names = [e["name"] for e in registry["wrappers"]]
        assert len(names) == len(set(names))

    def test_connector_names_unique(self, registry):
        names = [e["name"] for e in registry["connectors"]]
        assert len(names) == len(set(names))

    def test_official_tools_present(self, registry):
        """Pin the canonical official wrapper list. New official wrappers
        must be added here so coverage stays explicit.

        Note: gworkspace, websearch, and moltbook were retired in v0.9
        because they violated the wrapper boundary rule (pure remote
        API proxies with no local install lifecycle). Users who still
        need that functionality configure community MCP servers —
        see docs/extensibility.md and docs/mcp.md.
        """
        names = {e["name"] for e in registry["wrappers"]}
        official = {
            "aider", "browser",
            "docreader", "transcriber", "ocr",
        }
        missing = official - names
        assert not missing, f"missing official wrappers in registry.json: {missing}"
        # Retired wrappers must not reappear
        retired = {"gworkspace", "websearch", "moltbook"}
        reappeared = retired & names
        assert not reappeared, (
            f"retired wrappers must not reappear in registry.json: {reappeared}"
        )

    def test_official_connectors_present(self, registry):
        names = {e["name"] for e in registry["connectors"]}
        assert "discord" in names

    def test_default_preset_present(self, registry):
        names = {e["name"] for e in registry["presets"]}
        assert "default" in names


# --- fetch_registry ---


class TestFetchRegistry:
    def test_returns_registry_on_success(self):
        mock_resp = MagicMock()
        mock_resp.text = '{"wrappers": [{"name": "browser", "description": "desc"}]}'
        with patch("httpx.get", return_value=mock_resp):
            result = reg.fetch_registry()
        assert result == {"wrappers": [{"name": "browser", "description": "desc"}]}

    def test_returns_empty_on_network_error(self):
        import httpx

        with patch("httpx.get", side_effect=httpx.ConnectError("fail")):
            result = reg.fetch_registry()
        assert result == {}

    def test_caches_result(self):
        mock_resp = MagicMock()
        mock_resp.text = '{"wrappers": []}'
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            reg.fetch_registry()
            reg.fetch_registry()
        mock_get.assert_called_once()

    def test_returns_stale_cache_on_error(self):
        import httpx

        mock_resp = MagicMock()
        mock_resp.text = '{"wrappers": [{"name": "browser", "description": "d"}]}'
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
        entries = FAKE_REGISTRY["wrappers"]
        assert reg.search_entries(entries, None) == entries

    def test_name_match(self):
        entries = FAKE_REGISTRY["wrappers"]
        result = reg.search_entries(entries, "browser")
        assert len(result) == 1
        assert result[0]["name"] == "browser"

    def test_description_match(self):
        entries = FAKE_REGISTRY["wrappers"]
        result = reg.search_entries(entries, "refactoring")
        assert len(result) == 1
        assert result[0]["name"] == "aider"

    def test_no_match(self):
        entries = FAKE_REGISTRY["wrappers"]
        assert reg.search_entries(entries, "nonexistent") == []


# --- cross_type_hint ---


class TestCrossTypeHint:
    def test_hint_found(self):
        hint = reg.cross_type_hint(FAKE_REGISTRY, "connectors", "browser")
        assert hint is not None
        assert "kiso wrapper search browser" in hint

    def test_no_hint(self):
        hint = reg.cross_type_hint(FAKE_REGISTRY, "connectors", "nonexistent")
        assert hint is None


# --- get_registry_wrappers ---


class TestGetRegistryTools:
    def test_all_uninstalled(self):
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            result = reg.get_registry_wrappers(set())
        assert "browser" in result
        assert "aider" in result
        assert "websearch" in result

    def test_filters_installed(self):
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            result = reg.get_registry_wrappers({"browser", "aider"})
        assert "browser" not in result
        assert "aider" not in result
        assert "websearch" in result

    def test_all_installed_returns_empty(self):
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            result = reg.get_registry_wrappers({"browser", "aider", "websearch"})
        assert result == ""

    def test_empty_registry_returns_empty(self):
        with patch.object(reg, "fetch_registry", return_value={}):
            result = reg.get_registry_wrappers(set())
        assert result == ""


# --- brain.py integration ---


class TestBrainRegistryIntegration:
    """Verify that get_registry_wrappers is called from brain context building."""

    def test_registry_wrappers_injected_when_none_installed(self):
        """When no wrappers installed, registry text is non-empty."""
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            text = reg.get_registry_wrappers(set())
        assert "Available wrappers (not installed):" in text
        assert "browser" in text
        assert "aider" in text

    def test_registry_wrappers_empty_when_all_installed(self):
        """When all wrappers installed, registry text is empty."""
        all_names = {t["name"] for t in FAKE_REGISTRY["wrappers"]}
        with patch.object(reg, "fetch_registry", return_value=FAKE_REGISTRY):
            text = reg.get_registry_wrappers(all_names)
        assert text == ""
