"""Tests for per-user MCP method allowlist (M1539).

The filter lives at :func:`kiso.brain.common.filter_mcp_catalog_by_user`
and is invoked by ``build_planner_messages`` after
``format_mcp_catalog`` renders the per-manager catalog text.
"""
from __future__ import annotations

import pytest

from kiso.brain.common import filter_mcp_catalog_by_user


_CATALOG = (
    "- github:create_issue — open a GitHub issue\n"
    "- github:list_issues — list open issues\n"
    "- search:web_search — freeform web search\n"
    "- filesystem:read_file — read a file\n"
)


class TestFilterMcpCatalogByUser:
    def test_admin_no_allow_sees_all(self):
        out = filter_mcp_catalog_by_user(
            _CATALOG, user_role="admin", user_mcp_allow=None,
        )
        assert "github:create_issue" in out
        assert "search:web_search" in out
        assert "filesystem:read_file" in out

    def test_user_no_allow_sees_nothing(self):
        out = filter_mcp_catalog_by_user(
            _CATALOG, user_role="user", user_mcp_allow=None,
        )
        assert out == ""

    def test_star_allows_all_regardless_of_role(self):
        for role in ("admin", "user"):
            out = filter_mcp_catalog_by_user(
                _CATALOG, user_role=role, user_mcp_allow="*",
            )
            assert "github:create_issue" in out
            assert "filesystem:read_file" in out

    def test_allowlist_filters(self):
        out = filter_mcp_catalog_by_user(
            _CATALOG,
            user_role="user",
            user_mcp_allow=["search:web_search", "github:list_issues"],
        )
        assert "search:web_search" in out
        assert "github:list_issues" in out
        assert "github:create_issue" not in out
        assert "filesystem:read_file" not in out

    def test_admin_with_allowlist_still_filters(self):
        out = filter_mcp_catalog_by_user(
            _CATALOG,
            user_role="admin",
            user_mcp_allow=["search:web_search"],
        )
        assert "search:web_search" in out
        assert "github:" not in out
        assert "filesystem:" not in out

    def test_unknown_allowlist_entries_silently_dropped(self, caplog):
        import logging
        with caplog.at_level(logging.DEBUG, logger="kiso.brain.common"):
            out = filter_mcp_catalog_by_user(
                _CATALOG,
                user_role="user",
                user_mcp_allow=["search:web_search", "not:real"],
            )
        assert "search:web_search" in out
        assert "not:real" not in out
        # Debug log mentions the dropped entry
        assert any("not:real" in rec.message for rec in caplog.records)

    def test_empty_catalog_stays_empty(self):
        assert filter_mcp_catalog_by_user(
            "", user_role="admin", user_mcp_allow="*"
        ) == ""


class TestUserDataclassMcpField:
    """Config parsing: [users.<name>] may now carry an `mcp` allowlist."""

    def _parse(self, toml_text: str):
        import tempfile
        from pathlib import Path
        from kiso.config import load_config

        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False
        ) as f:
            f.write(toml_text)
            tmp = Path(f.name)
        try:
            return load_config(tmp)
        finally:
            tmp.unlink()

    def test_admin_without_mcp_defaults_to_none(self):
        cfg = self._parse(
            '[tokens]\ncli = "t"\n'
            '[providers.openrouter]\nbase_url = "http://x"\n'
            '[users.alice]\nrole = "admin"\n'
        )
        assert cfg.users["alice"].mcp is None

    def test_user_with_allowlist(self):
        cfg = self._parse(
            '[tokens]\ncli = "t"\n'
            '[providers.openrouter]\nbase_url = "http://x"\n'
            '[users.bob]\nrole = "user"\nwrappers = "*"\n'
            'mcp = ["search:web_search", "github:list_issues"]\n'
        )
        assert cfg.users["bob"].mcp == [
            "search:web_search", "github:list_issues",
        ]

    def test_star_accepted(self):
        cfg = self._parse(
            '[tokens]\ncli = "t"\n'
            '[providers.openrouter]\nbase_url = "http://x"\n'
            '[users.carol]\nrole = "user"\nwrappers = "*"\nmcp = "*"\n'
        )
        assert cfg.users["carol"].mcp == "*"

    def test_invalid_mcp_value_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(
                '[tokens]\ncli = "t"\n'
                '[providers.openrouter]\nbase_url = "http://x"\n'
                '[users.dave]\nrole = "user"\nwrappers = "*"\n'
                'mcp = 42\n'
            )
