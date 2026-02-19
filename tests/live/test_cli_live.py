"""L5 — CLI lifecycle tests.

Real network calls (GitHub API, git clone), no LLM.
Gated behind ``--live-network`` flag.
"""

from __future__ import annotations

import shutil
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.live_network


# ---------------------------------------------------------------------------
# L5.1 — Skill search
# ---------------------------------------------------------------------------


class TestSkillSearch:
    def test_search_returns_results(self, capsys):
        """kiso skill search (no query) hits GitHub and doesn't crash."""
        from kiso.cli_skill import _skill_search

        _skill_search(Namespace(query=""))
        out = capsys.readouterr().out
        # Either results printed or "No skills found." — both are acceptable
        assert out.strip()

    def test_search_with_query(self, capsys):
        """kiso skill search 'search' filters results without error."""
        from kiso.cli_skill import _skill_search

        _skill_search(Namespace(query="search"))
        out = capsys.readouterr().out
        assert out.strip()


# ---------------------------------------------------------------------------
# L5.2 — Connector search
# ---------------------------------------------------------------------------


class TestConnectorSearch:
    def test_search_returns_results(self, capsys):
        """kiso connector search (no query) hits GitHub and doesn't crash."""
        from kiso.cli_connector import _connector_search

        _connector_search(Namespace(query=""))
        out = capsys.readouterr().out
        assert out.strip()


# ---------------------------------------------------------------------------
# L5.3 — Skill install + remove lifecycle
# ---------------------------------------------------------------------------


class TestSkillInstallRemove:
    def test_install_and_remove_official_skill(self, tmp_path: Path, capsys):
        """Install a real skill from kiso-run org → verify → remove → verify."""
        if shutil.which("git") is None:
            pytest.skip("git not found on PATH")

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Pick a skill that is likely to exist in the org.
        # "search" is the canonical example skill.
        skill_name = "search"

        args = Namespace(
            target=skill_name,
            name=None,
            no_deps=True,      # skip deps.sh in CI
            show_deps=False,
        )

        with (
            patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
            patch("kiso.cli_skill._require_admin"),
            patch("kiso.cli_skill.check_deps", return_value=[]),
        ):
            from kiso.cli_skill import _skill_install

            _skill_install(args)

        skill_dir = skills_dir / skill_name
        assert skill_dir.is_dir(), f"Skill dir not created: {skill_dir}"
        assert (skill_dir / "kiso.toml").exists(), "kiso.toml missing"
        assert not (skill_dir / ".installing").exists(), ".installing marker left behind"

        out = capsys.readouterr().out
        assert "installed successfully" in out.lower()

        # Now remove
        remove_args = Namespace(name=skill_name)
        with (
            patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
            patch("kiso.cli_skill._require_admin"),
        ):
            from kiso.cli_skill import _skill_remove

            _skill_remove(remove_args)

        assert not skill_dir.exists(), f"Skill dir not removed: {skill_dir}"
        out2 = capsys.readouterr().out
        assert "removed" in out2.lower()


# ---------------------------------------------------------------------------
# L5.4 — Connector install + remove lifecycle
# ---------------------------------------------------------------------------


class TestConnectorInstallRemove:
    def test_install_and_remove_official_connector(self, tmp_path: Path, capsys):
        """Install a real connector from kiso-run org → verify → remove → verify."""
        if shutil.which("git") is None:
            pytest.skip("git not found on PATH")

        connectors_dir = tmp_path / "connectors"
        connectors_dir.mkdir()

        connector_name = "discord"

        args = Namespace(
            target=connector_name,
            name=None,
            no_deps=True,
            show_deps=False,
        )

        with (
            patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
            patch("kiso.cli_skill._require_admin"),
        ):
            from kiso.cli_connector import _connector_install

            try:
                _connector_install(args)
            except SystemExit:
                pytest.skip(
                    f"connector-{connector_name} repo not available yet"
                )

        connector_dir = connectors_dir / connector_name
        assert connector_dir.is_dir(), f"Connector dir not created: {connector_dir}"
        assert (connector_dir / "kiso.toml").exists(), "kiso.toml missing"
        assert not (connector_dir / ".installing").exists(), ".installing marker left"

        out = capsys.readouterr().out
        assert "installed successfully" in out.lower()

        # Now remove
        remove_args = Namespace(name=connector_name)
        with (
            patch("kiso.cli_connector.CONNECTORS_DIR", connectors_dir),
            patch("kiso.cli_skill._require_admin"),
        ):
            from kiso.cli_connector import _connector_remove

            _connector_remove(remove_args)

        assert not connector_dir.exists(), f"Connector dir not removed: {connector_dir}"
        out2 = capsys.readouterr().out
        assert "removed" in out2.lower()
