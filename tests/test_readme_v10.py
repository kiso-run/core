"""Invariants on the project README and tutorial.

The README is the landing page for new users. For v0.10 it must reflect
the **skills + MCP** taxonomy and not teach the retired wrapper/recipe
model as current usage. The tutorial must walk a new user through the
two canonical flows for extending kiso post-v0.10 (install a skill,
install an MCP from URL).

Historical mentions of retired subsystems are allowed as long as they
are framed as history. Active usage strings (`kiso wrapper install`,
`kiso recipe ...`, "install a wrapper" imperatives) are not.
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
TUTORIAL = ROOT / "docs" / "tutorial.md"


class TestReadmeV10:

    def test_readme_exists(self) -> None:
        assert README.is_file()

    def test_mentions_skills_and_mcp(self) -> None:
        text = README.read_text(encoding="utf-8").lower()
        assert "skill" in text, "README must mention Agent Skills"
        assert "mcp" in text, "README must mention MCP"

    @pytest.mark.parametrize(
        "phrase",
        [
            "kiso wrapper install",
            "kiso wrapper create",
            "kiso wrapper remove",
            "kiso wrapper list",
            "kiso recipe install",
            "kiso recipe remove",
            "kiso recipe list",
            "Install a wrapper",
            "install a wrapper",
        ],
    )
    def test_no_active_retired_usage(self, phrase: str) -> None:
        text = README.read_text(encoding="utf-8")
        assert phrase not in text, (
            f"README still advertises retired usage string: {phrase!r}"
        )

    def test_quickstart_has_single_key_reference(self) -> None:
        text = README.read_text(encoding="utf-8")
        assert "OPENROUTER_API_KEY" in text, (
            "README quickstart must tell the user to set OPENROUTER_API_KEY"
        )

    def test_quickstart_mentions_doctor_and_msg(self) -> None:
        text = README.read_text(encoding="utf-8")
        assert "kiso doctor" in text, (
            "README quickstart must reference `kiso doctor` as a sanity check"
        )
        assert "kiso msg" in text, (
            "README quickstart must show `kiso msg`"
        )

    def test_links_to_docs_index(self) -> None:
        text = README.read_text(encoding="utf-8")
        assert "docs/index.md" in text, (
            "README must link the docs index as the entry point for docs"
        )


class TestTutorialV10:

    def test_tutorial_exists(self) -> None:
        assert TUTORIAL.is_file(), (
            "docs/tutorial.md is the v0.10 'first skill + first MCP' walk-through"
        )

    def test_covers_skill_install_from_url(self) -> None:
        text = TUTORIAL.read_text(encoding="utf-8")
        assert "kiso skill install" in text, (
            "tutorial must show the `kiso skill install --from-url` flow"
        )
        assert "--from-url" in text, (
            "tutorial must use the --from-url convention"
        )

    def test_covers_mcp_install_from_url(self) -> None:
        text = TUTORIAL.read_text(encoding="utf-8")
        assert "kiso mcp install" in text, (
            "tutorial must show the `kiso mcp install --from-url` flow"
        )

    def test_no_retired_subsystem_teaching(self) -> None:
        text = TUTORIAL.read_text(encoding="utf-8")
        for phrase in (
            "kiso wrapper install",
            "kiso recipe install",
            "kiso recipe remove",
        ):
            assert phrase not in text, (
                f"tutorial teaches retired subsystem: {phrase!r}"
            )
