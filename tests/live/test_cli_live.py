"""L5 — CLI lifecycle tests.

Real network calls (GitHub API, git clone), no LLM.
Gated behind ``--live-network`` flag.

These are optional smoke tests, not the primary semantic coverage for CLI
search/install behavior. Stronger deterministic coverage lives in the unit
tests for `cli.wrapper`, `cli.connector`, and `cli.plugin_ops`.
"""

from __future__ import annotations

import shutil
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.live_network


def _assert_search_smoke_output(out: str, *, kind: str, query: str) -> None:
    """Validate that live-network search returned structured, non-empty output.

    This is intentionally a smoke oracle: the registry is externally controlled,
    so the test should verify basic health without pinning exact entries.
    """
    lower = out.lower()
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert lines, "search produced no visible output"
    assert f"no {kind}s found" not in lower
    # Search results render as aligned list rows ("name  — description").
    assert any("—" in line or " - " in line for line in lines), (
        f"search output did not look like result rows: {out[:300]}"
    )
    if query:
        assert query.lower() in lower or kind in lower, (
            f"search output did not mention query '{query}' or {kind}: {out[:300]}"
        )


# ---------------------------------------------------------------------------
# L5.1 — Wrapper search
# ---------------------------------------------------------------------------


class TestWrapperSearch:
    """Optional smoke tests for remote registry-backed wrapper search."""

    def test_search_returns_results(self, capsys):
        """What: Runs 'kiso wrapper search' with an empty query string.

        Why: Validates the wrapper search fetches the remote registry and lists available wrappers.
        Expects: Non-empty stdout output.
        """
        from cli.wrapper import _wrapper_search as _skill_search

        try:
            _skill_search(Namespace(query=""))
        except SystemExit:
            pytest.skip("Registry fetch failed (network unavailable)")
        out = capsys.readouterr().out
        _assert_search_smoke_output(out, kind="wrapper", query="")

    def test_search_with_query(self, capsys):
        """What: Runs 'kiso wrapper search' with query 'search' to filter results.

        Why: Validates the wrapper search filters by keyword without crashing.
        Expects: Non-empty stdout output.
        """
        from cli.wrapper import _wrapper_search as _skill_search

        try:
            _skill_search(Namespace(query="search"))
        except SystemExit:
            pytest.skip("Registry fetch failed (network unavailable)")
        out = capsys.readouterr().out
        _assert_search_smoke_output(out, kind="wrapper", query="search")


# ---------------------------------------------------------------------------
# L5.2 — Connector search
# ---------------------------------------------------------------------------


class TestConnectorSearch:
    """Optional smoke tests for remote registry-backed connector search."""

    def test_search_returns_results(self, capsys):
        """What: Runs 'kiso connector search' with an empty query string.

        Why: Validates the connector search fetches the remote registry and lists connectors.
        Expects: Non-empty stdout output.
        """
        from cli.connector import _connector_search

        try:
            _connector_search(Namespace(query=""))
        except SystemExit:
            pytest.skip("Registry fetch failed (network unavailable)")
        out = capsys.readouterr().out
        _assert_search_smoke_output(out, kind="connector", query="")


# ---------------------------------------------------------------------------
# L5.3 — Wrapper install + remove lifecycle
# ---------------------------------------------------------------------------


class TestWrapperInstallRemove:
    def test_install_and_remove_official_wrapper(self, tmp_path: Path, capsys):
        """What: Installs the 'websearch' wrapper from kiso-run org, verifies the directory, then removes it.

        Why: Validates the full wrapper install/remove lifecycle with a real git clone.
        Expects: After install: dir exists, kiso.toml present, no .installing marker. After remove: dir gone.
        """
        if shutil.which("git") is None:
            pytest.skip("git not found on PATH")

        skills_dir = tmp_path / "wrappers"
        skills_dir.mkdir()

        # Pick a wrapper that is likely to exist in the org.
        # "websearch" is the canonical example wrapper.
        skill_name = "websearch"

        args = Namespace(
            target=skill_name,
            name=None,
            no_deps=True,      # skip deps.sh in CI
            show_deps=False,
        )

        with (
            patch("cli.wrapper.WRAPPERS_DIR", skills_dir),
            patch("cli.wrapper._require_admin"),
            patch("cli.wrapper.check_deps", return_value=[]),
        ):
            from cli.wrapper import _wrapper_install as _skill_install

            _skill_install(args)

        skill_dir = skills_dir / skill_name
        assert skill_dir.is_dir(), f"Wrapper dir not created: {skill_dir}"
        assert (skill_dir / "kiso.toml").exists(), "kiso.toml missing"
        assert not (skill_dir / ".installing").exists(), ".installing marker left behind"

        out = capsys.readouterr().out
        assert "installed successfully" in out.lower()

        # Now remove
        remove_args = Namespace(name=skill_name)
        with (
            patch("cli.wrapper.WRAPPERS_DIR", skills_dir),
            patch("cli.wrapper._require_admin"),
        ):
            from cli.wrapper import _wrapper_remove as _skill_remove

            _skill_remove(remove_args)

        assert not skill_dir.exists(), f"Wrapper dir not removed: {skill_dir}"
        out2 = capsys.readouterr().out
        assert "removed" in out2.lower()


# ---------------------------------------------------------------------------
# L5.4 — Connector install + remove lifecycle
# ---------------------------------------------------------------------------


class TestConnectorInstallRemove:
    def test_install_and_remove_official_connector(self, tmp_path: Path, capsys):
        """What: Installs the 'discord' connector from kiso-run org, verifies the directory, then removes it.

        Why: Validates the full connector install/remove lifecycle with a real git clone.
        Expects: After install: dir exists, kiso.toml present, no .installing marker. After remove: dir gone.
        """
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
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("cli.connector.require_admin"),
        ):
            from cli.connector import _connector_install

            _connector_install(args)

        connector_dir = connectors_dir / connector_name
        assert connector_dir.is_dir(), f"Connector dir not created: {connector_dir}"
        assert (connector_dir / "kiso.toml").exists(), "kiso.toml missing"
        assert not (connector_dir / ".installing").exists(), ".installing marker left"

        out = capsys.readouterr().out
        assert "installed successfully" in out.lower()

        # Now remove
        remove_args = Namespace(name=connector_name)
        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("cli.connector.require_admin"),
        ):
            from cli.connector import _connector_remove

            _connector_remove(remove_args)

        assert not connector_dir.exists(), f"Connector dir not removed: {connector_dir}"
        out2 = capsys.readouterr().out
        assert "removed" in out2.lower()


# ---------------------------------------------------------------------------
# L5.5 — Install nonexistent wrapper/connector → clean error
# ---------------------------------------------------------------------------


class TestWrapperInstallNotFound:
    def test_install_nonexistent_wrapper(self, tmp_path: Path, capsys):
        """What: Attempts to install a nonexistent wrapper ('nonexistent-xyz-999').

        Why: Validates the CLI shows a clean user-facing 'not found' error, not raw git stderr.
        Expects: SystemExit raised, output contains 'not found' and 'kiso-run', no 'fatal:'.
        """
        if shutil.which("git") is None:
            pytest.skip("git not found on PATH")

        skills_dir = tmp_path / "wrappers"
        skills_dir.mkdir()

        args = Namespace(
            target="nonexistent-xyz-999",
            name=None,
            no_deps=True,
            show_deps=False,
        )

        with (
            patch("cli.wrapper.WRAPPERS_DIR", skills_dir),
            patch("cli.wrapper._require_admin"),
        ):
            from cli.wrapper import _wrapper_install as _skill_install

            with pytest.raises(SystemExit):
                _skill_install(args)

        out = capsys.readouterr().out
        assert "not found" in out.lower()
        assert "kiso-run" in out
        # Must NOT show raw git stderr
        assert "fatal:" not in out.lower()


class TestConnectorInstallNotFound:
    def test_install_nonexistent_connector(self, tmp_path: Path, capsys):
        """What: Attempts to install a nonexistent connector ('nonexistent-xyz-999').

        Why: Validates the CLI shows a clean user-facing 'not found' error, not raw git stderr.
        Expects: SystemExit raised, output contains 'not found' and 'kiso-run', no 'fatal:'.
        """
        if shutil.which("git") is None:
            pytest.skip("git not found on PATH")

        connectors_dir = tmp_path / "connectors"
        connectors_dir.mkdir()

        args = Namespace(
            target="nonexistent-xyz-999",
            name=None,
            no_deps=True,
            show_deps=False,
        )

        with (
            patch("cli.connector.CONNECTORS_DIR", connectors_dir),
            patch("cli.connector.require_admin"),
        ):
            from cli.connector import _connector_install

            with pytest.raises(SystemExit):
                _connector_install(args)

        out = capsys.readouterr().out
        assert "not found" in out.lower()
        assert "kiso-run" in out
        # Must NOT show raw git stderr
        assert "fatal:" not in out.lower()
