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

    def test_npm_playwright_mcp_auto_args(self):
        """`@playwright/mcp` needs three auto-injected flags:
          - --browser=chromium → use the bundled Chromium (no
            system Chrome dependency).
          - --isolated → fresh temp userDataDir per launch (avoids
            profile-lock conflicts on sequential or concurrent
            kiso MCP calls).
          - --output-dir ${session:workspace}/pub → screenshots /
            PDFs land in the session's auto-publish dir, enabling
            cross-plan MCP handoff (screenshot → OCR).
        All three must be in the resolved args; missing any
        produces a regression we have already paid for."""
        r = resolve_from_url("npm:@playwright/mcp")
        assert r.command == "npx"
        assert "@playwright/mcp" in r.args
        assert "--browser=chromium" in r.args, (
            "missing --browser=chromium → install fails on hosts "
            "without system Chrome at /opt/google/chrome/chrome"
        )
        assert "--isolated" in r.args, (
            "missing --isolated → sequential extended browser tests "
            "fail with 'Browser is already in use' profile-lock errors"
        )
        # --output-dir must be followed by the workspace-token path
        # (two consecutive args, since the option takes a value).
        assert "--output-dir" in r.args, (
            "missing --output-dir → screenshots land in a tmp dir "
            "the next plan turn can't see, breaking cross-plan "
            "handoff to OCR / file-consuming MCPs"
        )
        idx = r.args.index("--output-dir")
        assert r.args[idx + 1] == "${session:workspace}/pub", (
            f"--output-dir must point to ${{session:workspace}}/pub; "
            f"got {r.args[idx + 1]!r}"
        )
        # cwd → relative paths in MCP method args (e.g. the planner
        # emits `filename: "pub/screenshot.png"`) resolve against the
        # session workspace, not the parent process's cwd
        # (typically the Docker WORKDIR `/opt/kiso/`). Without this,
        # browser-mcp resolves relative paths against the WORKDIR and
        # the file ends up outside the session, breaking handoff.
        assert r.cwd == "${session:workspace}", (
            f"@playwright/mcp must launch with "
            f"cwd=${{session:workspace}}; got {r.cwd!r}"
        )


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
                "env": {"KISO_SECRET": "stolen"},
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
