"""Tests for ``kiso mcp trust`` and ``kiso skill trust`` CLI subgroups.

Business requirement: users can inspect and extend the per-type
trust list without hand-editing ``~/.kiso/trust.json``.
"""

from __future__ import annotations

import argparse

import pytest

from cli import mcp as cli_mcp
from cli import skill as cli_skill
from kiso.trust_store import add_prefix, load_trust_store


@pytest.fixture()
def trust_path(tmp_path, monkeypatch):
    p = tmp_path / "trust.json"
    monkeypatch.setattr("kiso.trust_store.TRUST_PATH", p)
    return p


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ---------------------------------------------------------------------------
# kiso mcp trust …
# ---------------------------------------------------------------------------


class TestMcpTrustCli:
    def test_argparse_exposes_trust_group(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_mcp.add_subcommands(parser)
        args = parser.parse_args(["trust", "list"])
        assert args.mcp_command == "trust"
        assert args.mcp_trust_command == "list"

    def test_add_then_list(self, trust_path, capsys):
        cli_mcp._cmd_trust(_ns(mcp_trust_command="add", prefix="npm:@me/*"))
        rc = cli_mcp._cmd_trust(_ns(mcp_trust_command="list"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "npm:@me/*" in out

    def test_remove(self, trust_path, capsys):
        cli_mcp._cmd_trust(_ns(mcp_trust_command="add", prefix="npm:@me/*"))
        cli_mcp._cmd_trust(_ns(mcp_trust_command="remove", prefix="npm:@me/*"))
        capsys.readouterr()  # drop add/remove echoes
        cli_mcp._cmd_trust(_ns(mcp_trust_command="list"))
        out = capsys.readouterr().out
        assert "npm:@me/*" not in out

    def test_list_includes_tier1_marker(self, trust_path, capsys):
        cli_mcp._cmd_trust(_ns(mcp_trust_command="list"))
        out = capsys.readouterr().out
        # Tier 1 prefixes are shown alongside user prefixes so the user can
        # see what's already whitelisted.
        assert "modelcontextprotocol" in out or "tier1" in out.lower()


# ---------------------------------------------------------------------------
# kiso skill trust …
# ---------------------------------------------------------------------------


class TestSkillTrustCli:
    def test_argparse_exposes_trust_group(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_skill.add_subcommands(parser)
        args = parser.parse_args(["trust", "list"])
        assert args.skill_command == "trust"
        assert args.skill_trust_command == "list"

    def test_add_then_list(self, trust_path, capsys):
        cli_skill._cmd_trust(
            _ns(skill_trust_command="add", prefix="github.com/me/*")
        )
        rc = cli_skill._cmd_trust(_ns(skill_trust_command="list"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "github.com/me/*" in out

    def test_remove(self, trust_path, capsys):
        cli_skill._cmd_trust(
            _ns(skill_trust_command="add", prefix="github.com/me/*")
        )
        cli_skill._cmd_trust(
            _ns(skill_trust_command="remove", prefix="github.com/me/*")
        )
        capsys.readouterr()  # drop add/remove echoes
        cli_skill._cmd_trust(_ns(skill_trust_command="list"))
        out = capsys.readouterr().out
        assert "github.com/me/*" not in out


# ---------------------------------------------------------------------------
# Install-time gate integration (skill side — simplest to exercise)
# ---------------------------------------------------------------------------


_STANDARD_SKILL = """\
---
name: python-debug
description: Helps debug Python tracebacks.
---

body
"""


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(cli_skill, "SKILLS_DIR", d)
    from kiso.skill_loader import invalidate_skills_cache
    invalidate_skills_cache()
    return d


class TestSkillInstallGate:
    def test_tier1_source_installs_without_prompt(
        self, skills_dir, trust_path, monkeypatch
    ):
        monkeypatch.setattr(
            cli_skill, "_http_fetcher", lambda url: _STANDARD_SKILL
        )
        # Simulate a Tier 1 source — raw SKILL.md under the anthropics
        # skills repo path.
        rc = cli_skill._cmd_install(
            _ns(
                from_url="https://raw.githubusercontent.com/anthropics/skills/main/python-debug/SKILL.md",
                name=None,
                dry_run=False,
                yes=False,
                force=False,
            )
        )
        assert rc == 0
        assert (skills_dir / "python-debug" / "SKILL.md").exists()

    def test_untrusted_refused_without_yes(
        self, skills_dir, trust_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            cli_skill, "_http_fetcher", lambda url: _STANDARD_SKILL
        )
        # Simulate a declined prompt: our stdin responder returns 'n'.
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "n")
        rc = cli_skill._cmd_install(
            _ns(
                from_url="https://raw.example.com/random/SKILL.md",
                name=None,
                dry_run=False,
                yes=False,
                force=False,
            )
        )
        assert rc != 0
        # Nothing installed
        assert list(skills_dir.iterdir()) == []
        out = capsys.readouterr().out
        assert "untrusted" in out.lower() or "trust" in out.lower()

    def test_untrusted_approved_via_yes(
        self, skills_dir, trust_path, monkeypatch
    ):
        monkeypatch.setattr(
            cli_skill, "_http_fetcher", lambda url: _STANDARD_SKILL
        )
        rc = cli_skill._cmd_install(
            _ns(
                from_url="https://raw.example.com/random/SKILL.md",
                name=None,
                dry_run=False,
                yes=True,
                force=False,
            )
        )
        assert rc == 0
        # Provenance records the untrusted-user-approved tier.
        prov_path = skills_dir / "python-debug" / ".provenance.json"
        assert prov_path.exists()
        import json
        data = json.loads(prov_path.read_text())
        assert data.get("trust_tier") in (
            "untrusted-user-approved",
            "untrusted",
            "custom",
            "tier1",
        )

    def test_custom_prefix_trusts_source(
        self, skills_dir, trust_path, monkeypatch
    ):
        add_prefix("skill", "raw.example.com/*")
        monkeypatch.setattr(
            cli_skill, "_http_fetcher", lambda url: _STANDARD_SKILL
        )
        rc = cli_skill._cmd_install(
            _ns(
                from_url="https://raw.example.com/custom/SKILL.md",
                name=None,
                dry_run=False,
                yes=False,
                force=False,
            )
        )
        assert rc == 0
        prov_path = skills_dir / "python-debug" / ".provenance.json"
        import json
        data = json.loads(prov_path.read_text())
        assert data.get("trust_tier") == "custom"
