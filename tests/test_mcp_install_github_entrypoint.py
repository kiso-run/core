"""Regression tests for M1641.

`_resolve_github` must derive the venv binary name from the cloned
repo's `pyproject.toml` `[project.scripts]` instead of assuming
the binary equals the server config name.

Selection rules (deterministic):
- 0 scripts → `InstallResolverError`
- 1 script → that one
- >1 scripts → prefer `kiso-*-mcp`, then `*-mcp`, then alphabetical

These are unit-tier tests (no clone, no install): we test the pure
selector and we test that `_resolve_github` wires a post-install
command resolver that the CLI invokes after `pre_install` runs.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kiso.mcp.install import (
    InstallResolverError,
    _resolve_github,
    _select_entry_point_from_scripts,
)


class TestSelectEntryPointFromScripts:
    """Pure deterministic selection over a `[project.scripts]` map."""

    def test_zero_scripts_raises(self):
        with pytest.raises(InstallResolverError) as exc:
            _select_entry_point_from_scripts({})
        assert "project.scripts" in str(exc.value).lower() or "entry-point" in str(exc.value).lower()

    def test_single_script_chosen(self):
        scripts = {"kiso-ocr-mcp": "kiso_ocr.cli:main"}
        assert _select_entry_point_from_scripts(scripts) == "kiso-ocr-mcp"

    def test_prefers_kiso_dash_mcp_when_multiple(self):
        scripts = {
            "helper": "pkg.helper:main",
            "kiso-foo-mcp": "pkg.cli:main",
            "thing": "pkg.thing:main",
        }
        assert _select_entry_point_from_scripts(scripts) == "kiso-foo-mcp"

    def test_prefers_dash_mcp_when_no_kiso_match(self):
        scripts = {
            "helper": "pkg.helper:main",
            "foo-mcp": "pkg.cli:main",
            "tool": "pkg.tool:main",
        }
        assert _select_entry_point_from_scripts(scripts) == "foo-mcp"

    def test_multiple_kiso_dash_mcp_alphabetical(self):
        scripts = {
            "kiso-zeta-mcp": "x:main",
            "kiso-alpha-mcp": "y:main",
        }
        assert _select_entry_point_from_scripts(scripts) == "kiso-alpha-mcp"

    def test_multiple_dash_mcp_alphabetical(self):
        scripts = {
            "zeta-mcp": "x:main",
            "alpha-mcp": "y:main",
        }
        assert _select_entry_point_from_scripts(scripts) == "alpha-mcp"

    def test_no_mcp_pattern_falls_back_to_alphabetical(self):
        scripts = {
            "zoo": "x:main",
            "apple": "y:main",
        }
        assert _select_entry_point_from_scripts(scripts) == "apple"


class TestResolveGithubPostInstallHook:
    """`_resolve_github` must defer the venv binary name until after
    the editable install runs and `pyproject.toml` is parseable."""

    def test_resolved_exposes_post_install_resolver(self):
        res = _resolve_github("https://github.com/kiso-run/ocr-mcp", name_hint="ocr")
        assert res.post_install_command_resolver is not None, (
            "_resolve_github must set post_install_command_resolver "
            "so the CLI can finalize `command` after the editable "
            "install populates the venv."
        )

    def test_post_install_resolver_reads_pyproject(self, tmp_path: Path, monkeypatch):
        """The resolver, given a clone with a real pyproject.toml,
        must select the right entry-point and produce a venv path."""
        from kiso.mcp import install as install_mod

        clone_dir = tmp_path / "ocr-clone"
        clone_dir.mkdir()
        (clone_dir / "pyproject.toml").write_text(
            textwrap.dedent("""
                [project]
                name = "kiso-ocr-mcp"
                version = "0.1.0"

                [project.scripts]
                kiso-ocr-mcp = "kiso_ocr.cli:main"
            """).strip()
        )

        # Point MCP_SERVERS_DIR at tmp so the resolver builds the
        # clone path under our control.
        monkeypatch.setattr(install_mod, "MCP_SERVERS_DIR", tmp_path)

        # Re-resolve so the captured clone_dir matches our fixture.
        res = install_mod._resolve_github(
            "https://github.com/kiso-run/ocr-mcp",
            name_hint="ocr-clone",  # so MCP_SERVERS_DIR/<name> == clone_dir
        )
        finalized_command = res.post_install_command_resolver()
        assert finalized_command.endswith("/.venv/bin/kiso-ocr-mcp")

    def test_post_install_resolver_errors_on_missing_pyproject(self, tmp_path: Path, monkeypatch):
        from kiso.mcp import install as install_mod

        clone_dir = tmp_path / "broken-clone"
        clone_dir.mkdir()  # no pyproject.toml at all

        monkeypatch.setattr(install_mod, "MCP_SERVERS_DIR", tmp_path)

        res = install_mod._resolve_github(
            "https://github.com/somebody/broken",
            name_hint="broken-clone",
        )
        with pytest.raises(InstallResolverError):
            res.post_install_command_resolver()
