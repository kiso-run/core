"""Tests for the install-time trust store and per-type trust modules.

Business requirement: sources matching hardcoded Tier-1 prefixes
install silently; everything else must pass a safety gate (warning
+ explicit user confirmation, or ``--yes`` bypass). Users extend
the trust list via ``kiso mcp trust`` and ``kiso skill trust``, in
``~/.kiso/trust.json`` — a single file shared by both types.

Trust tiers:
- ``"tier1"`` — source matches a hardcoded prefix in
  ``kiso/mcp/trust.py`` or ``kiso/skill_trust.py``.
- ``"custom"`` — source matches a user-added prefix in
  ``~/.kiso/trust.json``.
- ``"untrusted"`` — neither; requires explicit approval to install.

Plus skill-specific risk factors detected from the staged skill
directory (``scripts/`` present, wide ``allowed-tools``, oversized
SKILL.md or assets), surfaced to the prompt but not themselves a
block — trust says "yes/no install"; risk says "here's why you
should look before saying yes".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiso.mcp import trust as mcp_trust
from kiso import skill_trust
from kiso.trust_store import (
    TrustStore,
    add_prefix,
    load_trust_store,
    matches_any_prefix,
    remove_prefix,
    save_trust_store,
)


# ---------------------------------------------------------------------------
# trust_store — load/save + shared prefix match
# ---------------------------------------------------------------------------


@pytest.fixture()
def trust_path(tmp_path, monkeypatch):
    p = tmp_path / "trust.json"
    monkeypatch.setattr("kiso.trust_store.TRUST_PATH", p)
    return p


class TestLoadSave:
    def test_load_when_missing_returns_empty_store(self, trust_path):
        store = load_trust_store()
        assert store == TrustStore(mcp=[], skill=[])

    def test_save_then_load_roundtrip(self, trust_path):
        store = TrustStore(
            mcp=["npm:@acme/*"],
            skill=["github.com/alice/skills/*"],
        )
        save_trust_store(store)
        reloaded = load_trust_store()
        assert reloaded == store

    def test_save_is_atomic_via_tempfile_rename(self, trust_path):
        # Write an initial store so the file exists.
        save_trust_store(TrustStore(mcp=["a"], skill=[]))
        initial = trust_path.read_text()

        # Saving a new value must either leave the file at the old
        # content or the new one — never a partial write.
        save_trust_store(TrustStore(mcp=["a", "b"], skill=[]))
        assert trust_path.exists()
        data = json.loads(trust_path.read_text())
        assert data["mcp"] == ["a", "b"]
        assert data != json.loads(initial)  # actually updated

    def test_save_creates_parent_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "dir" / "trust.json"
        monkeypatch.setattr("kiso.trust_store.TRUST_PATH", nested)
        save_trust_store(TrustStore(mcp=["x"], skill=[]))
        assert nested.exists()


class TestMatchesAnyPrefix:
    def test_exact_match(self):
        assert matches_any_prefix("npm:foo", ["npm:foo"]) is True

    def test_glob_prefix_star(self):
        assert matches_any_prefix("npm:@acme/tool", ["npm:@acme/*"]) is True
        assert matches_any_prefix("npm:@other/tool", ["npm:@acme/*"]) is False

    def test_empty_prefix_list(self):
        assert matches_any_prefix("anything", []) is False

    def test_longest_prefix_wins_is_not_required(self):
        # Multiple overlapping prefixes — any match returns True.
        assert matches_any_prefix(
            "github.com/kiso-run/foo-mcp",
            ["github.com/*", "github.com/kiso-run/*-mcp"],
        ) is True

    def test_source_case_sensitive(self):
        # We do NOT downcase — GitHub is case-preserving and user prefixes
        # must match exactly.
        assert matches_any_prefix("GitHub.com/acme/x", ["github.com/*"]) is False


class TestAddRemovePrefix:
    def test_add_new_prefix(self, trust_path):
        add_prefix("mcp", "npm:@custom/*")
        store = load_trust_store()
        assert "npm:@custom/*" in store.mcp

    def test_add_duplicate_is_noop(self, trust_path):
        add_prefix("mcp", "npm:@custom/*")
        add_prefix("mcp", "npm:@custom/*")
        store = load_trust_store()
        assert store.mcp.count("npm:@custom/*") == 1

    def test_remove_existing_prefix(self, trust_path):
        add_prefix("skill", "github.com/me/*")
        remove_prefix("skill", "github.com/me/*")
        store = load_trust_store()
        assert "github.com/me/*" not in store.skill

    def test_remove_missing_prefix_is_noop(self, trust_path):
        # No error; remove_prefix is idempotent.
        remove_prefix("skill", "github.com/me/*")
        store = load_trust_store()
        assert store.skill == []

    def test_unknown_type_rejected(self, trust_path):
        with pytest.raises(ValueError):
            add_prefix("wrappers", "x")


# ---------------------------------------------------------------------------
# kiso.mcp.trust — Tier 1 prefixes for MCP sources
# ---------------------------------------------------------------------------


class TestMcpTrust:
    def test_tier1_modelcontextprotocol_npm(self, trust_path):
        assert mcp_trust.is_trusted("npm:@modelcontextprotocol/server-github") == "tier1"

    def test_tier1_playwright_npm(self, trust_path):
        assert mcp_trust.is_trusted("npm:@playwright/mcp") == "tier1"

    def test_tier1_kiso_run_mcp_repo(self, trust_path):
        assert mcp_trust.is_trusted("github.com/kiso-run/search-mcp") == "tier1"

    def test_untrusted_npm_scope(self, trust_path):
        assert mcp_trust.is_trusted("npm:@random-vendor/mcp") == "untrusted"

    def test_untrusted_random_github(self, trust_path):
        assert mcp_trust.is_trusted("github.com/someone/random-mcp") == "untrusted"

    def test_user_custom_prefix_elevates_to_custom(self, trust_path):
        add_prefix("mcp", "npm:@custom-vendor/*")
        assert (
            mcp_trust.is_trusted("npm:@custom-vendor/mcp-thing") == "custom"
        )

    def test_user_custom_does_not_shadow_tier1(self, trust_path):
        # A source that also matches Tier 1 should stay "tier1".
        add_prefix("mcp", "npm:*")
        assert (
            mcp_trust.is_trusted("npm:@modelcontextprotocol/server-github")
            == "tier1"
        )


class TestMcpSourceKey:
    """``source_key_for_url`` normalises a user-supplied URL into the
    trust-prefix shape used by ``is_trusted``. Tested via the public
    resolver-URL forms the install flow actually sees.
    """

    def test_npm_pkg_pseudo_url(self):
        assert mcp_trust.source_key_for_url(
            "npm:@modelcontextprotocol/server-github"
        ) == "npm:@modelcontextprotocol/server-github"

    def test_npmjs_com_url(self):
        assert mcp_trust.source_key_for_url(
            "https://www.npmjs.com/package/@modelcontextprotocol/server-github"
        ) == "npm:@modelcontextprotocol/server-github"

    def test_github_url(self):
        assert mcp_trust.source_key_for_url(
            "https://github.com/kiso-run/search-mcp"
        ) == "github.com/kiso-run/search-mcp"


# ---------------------------------------------------------------------------
# kiso.skill_trust — Tier 1 prefixes + risk factors
# ---------------------------------------------------------------------------


class TestSkillTrust:
    def test_tier1_anthropic_skills_repo(self, trust_path):
        assert (
            skill_trust.is_trusted("github.com/anthropics/skills/writing-style")
            == "tier1"
        )

    def test_tier1_kiso_run_skills_repo(self, trust_path):
        assert (
            skill_trust.is_trusted("github.com/kiso-run/skills/x")
            == "tier1"
        )

    def test_untrusted_random_github(self, trust_path):
        assert (
            skill_trust.is_trusted("github.com/someone/random-skill")
            == "untrusted"
        )

    def test_user_custom_prefix(self, trust_path):
        add_prefix("skill", "github.com/me/*")
        assert (
            skill_trust.is_trusted("github.com/me/custom-skill") == "custom"
        )


_STANDARD_SKILL = """\
---
name: python-debug
description: Helps debug Python tracebacks.
---

body
"""

_SKILL_WITH_RISKY_TOOLS = """\
---
name: dangerous
description: broad powers
allowed-tools: Bash(*) Write(*) Edit(*)
---

body
"""


class TestRiskFactors:
    def test_clean_skill_has_no_risk_factors(self, tmp_path):
        d = tmp_path / "python-debug"
        d.mkdir()
        (d / "SKILL.md").write_text(_STANDARD_SKILL)
        assert skill_trust.detect_risk_factors(d) == []

    def test_scripts_directory_is_risk_factor(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        (d / "SKILL.md").write_text(_STANDARD_SKILL)
        (d / "scripts").mkdir()
        (d / "scripts" / "run.sh").write_text("#!/bin/sh\n")
        risks = skill_trust.detect_risk_factors(d)
        assert any("scripts" in r for r in risks)

    def test_wide_allowed_tools_is_risk_factor(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        (d / "SKILL.md").write_text(_SKILL_WITH_RISKY_TOOLS)
        risks = skill_trust.detect_risk_factors(d)
        assert any("allowed-tools" in r.lower() for r in risks)

    def test_oversized_skill_md_is_risk_factor(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        big_body = _STANDARD_SKILL + ("x" * 60_000)
        (d / "SKILL.md").write_text(big_body)
        risks = skill_trust.detect_risk_factors(d)
        assert any("SKILL.md" in r for r in risks)

    def test_oversized_assets_is_risk_factor(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        (d / "SKILL.md").write_text(_STANDARD_SKILL)
        (d / "assets").mkdir()
        (d / "assets" / "big.bin").write_bytes(b"\0" * (6 * 1024 * 1024))
        risks = skill_trust.detect_risk_factors(d)
        assert any("assets" in r.lower() or "asset" in r.lower() for r in risks)

    def test_risk_factors_for_single_file_skill(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text(_STANDARD_SKILL)
        # Single-file skill can't have scripts/ etc. — only the SKILL.md
        # size check applies. A minimal skill shows zero risk.
        assert skill_trust.detect_risk_factors(p) == []
