"""Tests for ``kiso.mcp.install.resolve_from_url``.

Every branch is covered without network by injecting a fake
``http_fetcher``. The git-clone resolver is exercised by asserting
the returned pre-install plan shape (not by actually cloning).
"""

from __future__ import annotations

import pytest

from kiso.mcp.install import (
    InstallResolverError,
    ResolvedServer,
    check_runtime_dependencies,
    resolve_from_url,
)


class TestNpmResolvers:
    def test_npm_pseudo_url(self):
        r = resolve_from_url("npm:@modelcontextprotocol/server-github")
        assert r.transport == "stdio"
        assert r.command == "npx"
        assert r.args == ["-y", "@modelcontextprotocol/server-github"]
        assert r.pre_install == []

    def test_npm_pseudo_url_unscoped(self):
        r = resolve_from_url("npm:some-mcp")
        assert r.command == "npx"
        assert r.args == ["-y", "some-mcp"]

    def test_npmjs_com_url(self):
        r = resolve_from_url(
            "https://www.npmjs.com/package/@modelcontextprotocol/server-github"
        )
        assert r.command == "npx"
        assert r.args == ["-y", "@modelcontextprotocol/server-github"]

    def test_empty_npm_rejected(self):
        with pytest.raises(InstallResolverError):
            resolve_from_url("npm:")


class TestPypiResolvers:
    def test_pypi_pseudo_url(self):
        r = resolve_from_url("pypi:some-mcp-server")
        assert r.command == "uvx"
        assert r.args == ["some-mcp-server"]

    def test_pypi_org_url(self):
        r = resolve_from_url("https://pypi.org/project/foo-mcp/")
        assert r.command == "uvx"
        assert r.args == ["foo-mcp"]

    def test_invalid_pypi_name(self):
        with pytest.raises(InstallResolverError):
            resolve_from_url("pypi:bad name with spaces")


class TestPulsemcpResolver:
    def test_pulsemcp_normalises_payload(self):
        def _fake(url: str) -> dict:
            return {
                "name": "moltbook",
                "command": "moltbook-mcp",
                "args": [],
                "env": {"MOLTBOOK_API_KEY": "$MOLTBOOK_TOKEN"},
            }

        r = resolve_from_url(
            "https://www.pulsemcp.com/servers/moltbook",
            http_fetcher=_fake,
        )
        assert r.name == "moltbook"
        assert r.command == "moltbook-mcp"
        assert r.env == {"MOLTBOOK_API_KEY": "$MOLTBOOK_TOKEN"}

    def test_pulsemcp_claude_desktop_shape(self):
        def _fake(url: str) -> dict:
            return {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                    }
                }
            }

        r = resolve_from_url(
            "https://www.pulsemcp.com/servers/github",
            http_fetcher=_fake,
        )
        assert r.name == "github"
        assert r.command == "npx"
        assert r.args == ["-y", "@modelcontextprotocol/server-github"]

    def test_pulsemcp_rejects_kiso_env(self):
        def _fake(url: str) -> dict:
            return {
                "name": "bad",
                "command": "foo",
                "env": {"KISO_LLM_API_KEY": "stolen"},
            }

        with pytest.raises(InstallResolverError, match="KISO_"):
            resolve_from_url(
                "https://www.pulsemcp.com/servers/bad",
                http_fetcher=_fake,
            )


class TestGithubResolver:
    def test_github_produces_clone_plan(self):
        r = resolve_from_url(
            "https://github.com/acamolese/google-search-console-mcp"
        )
        assert r.transport == "stdio"
        # Clone + venv + pip install steps
        assert any("git" in s[0] for s in r.pre_install)
        assert any("uv" in s[0] and "venv" in s for s in r.pre_install)
        assert any("uv" in s[0] and "pip" in s for s in r.pre_install)
        assert r.cwd is not None

    def test_github_missing_parts_rejected(self):
        with pytest.raises(InstallResolverError):
            resolve_from_url("https://github.com/")


class TestHttpServer:
    def test_http_from_raw_manifest(self):
        def _fake(url: str) -> dict:
            return {
                "name": "maps",
                "url": "https://mapstools.googleapis.com/mcp",
                "headers": {"X-Goog-Api-Key": "fake"},
            }

        r = resolve_from_url(
            "https://example.com/server.json",
            http_fetcher=_fake,
        )
        assert r.transport == "http"
        assert r.url == "https://mapstools.googleapis.com/mcp"
        assert r.headers == {"X-Goog-Api-Key": "fake"}


class TestUnknownUrls:
    def test_unknown_host_rejected(self):
        with pytest.raises(InstallResolverError, match="unrecognised"):
            resolve_from_url("https://example.com/whatever")

    def test_empty_url(self):
        with pytest.raises(InstallResolverError):
            resolve_from_url("")


class TestRuntimeDependencies:
    def test_returns_missing_list(self, monkeypatch):
        import kiso.mcp.install as install_mod

        monkeypatch.setattr(install_mod.shutil, "which", lambda name: None)
        missing = check_runtime_dependencies()
        assert set(missing) == {"uv", "npx"}

    def test_returns_empty_when_present(self, monkeypatch):
        import kiso.mcp.install as install_mod

        monkeypatch.setattr(
            install_mod.shutil, "which", lambda name: f"/usr/bin/{name}"
        )
        assert check_runtime_dependencies() == []
