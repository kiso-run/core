"""Regression test for M1639.

`_install_via_cli` in tests/functional/test_e2e_install_use.py must
include `--name <name>` in the `kiso mcp install` argv. Without the
flag the CLI resolver derives the server name from the package
(e.g. `npm:@playwright/mcp` → `mcp`) and the post-install sanity
check `_mcp_already_installed("browser")` fails because the config
has `[mcp.mcp]`, not `[mcp.browser]`.

This test is unit-tier (no network, no LLM): it patches
`subprocess.run` and asserts the install argv shape. It belongs in
the always-on tier so the bug class is caught at every commit, not
only when someone runs the extended tier.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from tests.functional.test_e2e_install_use import _install_via_cli


def _make_run_mock(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestInstallViaCliPassesName:
    """`_install_via_cli` must always pass `--name <name>` so the
    installed server lands at `[mcp.<name>]`, regardless of what the
    upstream resolver would derive from the package URL."""

    def test_install_argv_contains_name_flag(self):
        url = "npm:@playwright/mcp"
        name = "browser"

        install_call = _make_run_mock(returncode=0)
        list_call = _make_run_mock(returncode=0, stdout=f"{name}\n")

        with patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            side_effect=[install_call, list_call],
        ) as run_mock:
            _install_via_cli(url, name)

        # First call is the `kiso mcp install ...` invocation.
        install_argv = run_mock.call_args_list[0].args[0]
        assert "--name" in install_argv, (
            f"_install_via_cli must pass --name to `kiso mcp install`, "
            f"got argv: {install_argv}"
        )
        idx = install_argv.index("--name")
        assert install_argv[idx + 1] == name, (
            f"--name must be followed by the server name "
            f"({name!r}); got argv: {install_argv}"
        )

    def test_install_argv_still_contains_from_url_and_yes(self):
        """Regression guard: don't drop the existing flags while
        adding --name."""
        url = "https://github.com/kiso-run/ocr-mcp"
        name = "ocr"

        install_call = _make_run_mock(returncode=0)
        list_call = _make_run_mock(returncode=0, stdout=f"{name}\n")

        with patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            side_effect=[install_call, list_call],
        ) as run_mock:
            _install_via_cli(url, name)

        install_argv = run_mock.call_args_list[0].args[0]
        assert "--from-url" in install_argv
        assert install_argv[install_argv.index("--from-url") + 1] == url
        assert "--yes" in install_argv
