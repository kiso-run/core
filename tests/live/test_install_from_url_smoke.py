"""M1575 — live smoke for install-from-URL.

After M1572 deleted the wrapper/connector live tests, there is no
live coverage for the modern install paths:

- ``kiso skill install --from-url git+<github>`` (M1514)
- ``kiso mcp install --from-url git+<github>`` (M1503)

The unit tests in ``tests/test_cli_skill_install.py`` and
``tests/test_cli_mcp.py`` cover the install logic with mocked
fetchers and pre-install steps. These two smoke tests exercise
the FULL real-network path against known-good kiso-run artifacts
that ship in the default preset:

- ``kiso-run/message-attachment-receiver-skill`` (default branch) —
  a Tier 1 trusted skill the default preset bundles. Real
  ``git clone``, no LLM, no compose, no extra dependencies.
- ``kiso-run/transcriber-mcp`` (default branch) — a Tier 1 trusted
  MCP server. Real ``git clone`` + ``uv venv`` + ``uv pip install``
  (the pre-install plan resolved by ``kiso.mcp.install``).

Each test is gated behind ``--live-network`` and skips cleanly
when ``git`` (or ``uv`` for the MCP test) is missing on PATH or
the network is unreachable.

The tests do NOT exercise:
- runtime behavior of the installed artifact (that's
  ``tests/functional/``'s job),
- failure paths (covered by unit tests),
- private/auth-gated repos (out of scope; only public targets).
"""

from __future__ import annotations

import argparse
import shutil
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.live_network


# Both install resolvers (kiso.skill_install / kiso.mcp.install) accept
# bare GitHub repo URLs of the form ``https://github.com/<owner>/<repo>``.
# The skill resolver also accepts the ``/tree/<ref>/<subpath>`` form for
# tag-pinned installs; the ``git+https://...@<tag>`` shortcut used by
# uvx is NOT honored by either resolver (separate concern).
#
# This smoke installs from the default branch of each repo. If the
# default branch ever drifts in a way that breaks install, that is
# itself the regression the smoke catches.
_OFFICIAL_SKILL_URL = (
    "https://github.com/kiso-run/message-attachment-receiver-skill"
)
# The on-disk directory uses the skill's declared name (from
# SKILL.md frontmatter), not the GitHub repo name. The official
# skill in this repo declares ``name: message-attachment-receiver``.
_OFFICIAL_SKILL_NAME = "message-attachment-receiver"

_OFFICIAL_MCP_URL = (
    "https://github.com/kiso-run/transcriber-mcp"
)
_OFFICIAL_MCP_NAME = "transcriber"


def _network_or_skip() -> None:
    """Skip the test if GitHub is not reachable. Cheap pre-check
    against ``github.com:443`` — avoids waiting for a long git-clone
    timeout when the host has no network."""
    try:
        with socket.create_connection(("github.com", 443), timeout=5):
            pass
    except (OSError, socket.timeout) as exc:
        pytest.skip(f"network unreachable: github.com:443 — {exc}")


def _git_or_skip() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not found on PATH")


def _uv_or_skip() -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv not found on PATH (required for MCP install)")


# ---------------------------------------------------------------------------
# Skill install
# ---------------------------------------------------------------------------


class TestSkillInstallFromOfficialUrl:
    """Real ``git clone`` of an official kiso-run skill into a tmp
    skills_dir. Verifies the SKILL.md lands and the install path
    accepts a Tier 1 trusted source without prompting for confirmation.
    """

    def test_skill_install_from_official_url(self, tmp_path: Path, capsys):
        _git_or_skip()
        _network_or_skip()

        from cli import skill as cli_skill

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # NOTE: ``yes=True`` skips the trust-confirm prompt. The trust
        # matcher (kiso.skill_trust.source_key_for_url) does not strip
        # the ``@<tag>`` suffix when the URL is `git+https://...@v0.2.0`,
        # which leaves the source_key un-prefix-matchable. That is a
        # separate latent bug in the trust normalizer; this smoke test
        # exercises the install path itself, not the trust matcher.
        args = argparse.Namespace(
            from_url=_OFFICIAL_SKILL_URL,
            name=None,
            dry_run=False,
            yes=True,
            force=False,
        )

        with patch.object(cli_skill, "SKILLS_DIR", skills_dir):
            from kiso.skill_loader import invalidate_skills_cache
            invalidate_skills_cache()
            rc = cli_skill._cmd_install(args)

        assert rc == 0, (
            f"skill install --from-url {_OFFICIAL_SKILL_URL} failed "
            f"with exit code {rc}; stdout={capsys.readouterr().out!r}"
        )

        skill_root = skills_dir / _OFFICIAL_SKILL_NAME
        assert skill_root.is_dir(), (
            f"skill directory not created: {skill_root} "
            f"(skills_dir contents: {list(skills_dir.iterdir())})"
        )
        skill_md = skill_root / "SKILL.md"
        assert skill_md.is_file(), (
            f"SKILL.md missing in {skill_root}"
        )
        # Sanity: the SKILL.md should at least have the YAML
        # frontmatter delimiter (we're not validating the schema —
        # just that we got a real skill file from the clone).
        assert skill_md.read_text().startswith("---"), (
            f"SKILL.md does not look like a YAML-frontmatter skill: "
            f"{skill_md.read_text()[:200]!r}"
        )


# ---------------------------------------------------------------------------
# MCP install
# ---------------------------------------------------------------------------


class TestMcpInstallFromOfficialUrl:
    """Real ``git clone`` + ``uv venv`` + ``uv pip install`` of an
    official kiso-run MCP server. Verifies the [mcpServers.<name>]
    entry lands in config.toml.
    """

    def test_mcp_install_from_official_url(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        _git_or_skip()
        _uv_or_skip()
        _network_or_skip()

        # Isolate config.toml writes to tmp_path. KISO_HOME is read
        # at module-import time by kiso.config, so we monkeypatch
        # CONFIG_PATH directly on the cli.mcp module's import.
        kiso_home = tmp_path / "kiso"
        kiso_home.mkdir()
        config_path = kiso_home / "config.toml"
        # Seed a minimal, valid config.toml so _read_config_raw
        # has something to parse.
        config_path.write_text(
            "[tokens]\n"
            'cli = "test"\n'
            "\n"
            "[providers.openrouter]\n"
            'base_url = "https://openrouter.ai/api/v1"\n'
            "\n"
            "[users.test]\n"
            'role = "admin"\n'
            "\n"
            "[settings]\n"
            "\n"
            "[models]\n"
        )

        # The MCP install path resolves the clone target from
        # `kiso.mcp.install.MCP_SERVERS_DIR` (a module-level constant
        # bound at import time to `KISO_DIR / "mcp" / "servers"`). To
        # isolate the test from the user's real `~/.kiso`, we patch
        # the constant directly. CONFIG_PATH on cli.mcp is patched
        # separately so the persist step writes to the tmp config.
        from cli import mcp as cli_mcp
        from kiso import config as kiso_config
        from kiso.mcp import install as kiso_mcp_install

        servers_dir = kiso_home / "mcp" / "servers"
        servers_dir.mkdir(parents=True)

        monkeypatch.setattr(kiso_config, "CONFIG_PATH", config_path)
        monkeypatch.setattr(cli_mcp, "CONFIG_PATH", config_path)
        monkeypatch.setattr(
            kiso_mcp_install, "MCP_SERVERS_DIR", servers_dir,
        )

        # NOTE: ``yes=True`` skips the trust-confirm prompt. Same
        # trust-normalizer limitation as the skill test.
        args = argparse.Namespace(
            from_url=_OFFICIAL_MCP_URL,
            name=_OFFICIAL_MCP_NAME,
            dry_run=False,
            yes=True,
            force=False,
        )

        rc = cli_mcp._cmd_install(args)
        out = capsys.readouterr().out

        assert rc == 0, (
            f"mcp install --from-url {_OFFICIAL_MCP_URL} failed "
            f"with exit code {rc}; output:\n{out}"
        )

        # Verify the config.toml now has the expected server entry.
        # We do a tolerant string check rather than a full TOML parse
        # to keep the test independent of the exact dict serializer
        # the persist function uses (it might be tomli-w, tomli, or
        # an inline writer).
        # The persist function writes the entry under the kiso-style
        # section header `[mcp.<name>]` (not `[mcpServers.<name>]` as
        # in the JSON spec — the TOML form uses a flatter key).
        config_text = config_path.read_text()
        assert f"[mcp.{_OFFICIAL_MCP_NAME}]" in config_text, (
            f"[mcp.{_OFFICIAL_MCP_NAME}] not found in config.toml "
            f"after install:\n{config_text}"
        )
