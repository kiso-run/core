"""Regression tests for M1640 + M1643.

Extended-tier `test_browser_mcp_install_then_navigate` needs the
`chrome-for-testing` distribution installed for `@playwright/mcp
--browser=chromium`. `_ensure_chromium_installed()` shells out to
`npx @playwright/mcp install-browser chrome-for-testing` (no
sudo), idempotent because that subcommand is itself a no-op on
cache hit, and skips the test if the install errors (environment
problem, not a kiso bug).

These tests are unit-tier (no network, no LLM): patch
`subprocess.run` and assert the command shape.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def _make_run_mock(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestEnsureChromiumInstalled:
    """Behavioural contract for `_ensure_chromium_installed`."""

    def test_runs_playwright_mcp_install_browser_command(self):
        from tests.functional.test_e2e_install_use import _ensure_chromium_installed

        run_mock = _make_run_mock(returncode=0)
        with patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            return_value=run_mock,
        ) as run:
            _ensure_chromium_installed()

        assert run.call_count == 1, "expected exactly one subprocess.run call"
        argv = run.call_args.args[0]
        assert argv[0] == "npx"
        assert "@playwright/mcp" in argv
        assert "install-browser" in argv
        assert "chrome-for-testing" in argv

    def test_skips_test_when_install_fails(self):
        from tests.functional.test_e2e_install_use import _ensure_chromium_installed

        run_mock = _make_run_mock(returncode=1, stderr="network error")
        with patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            return_value=run_mock,
        ):
            with pytest.raises(pytest.skip.Exception):
                _ensure_chromium_installed()


class TestSkipIfMissingPrereqsBrowserFlag:
    """`_skip_if_missing_prereqs(need_browser=True)` must invoke
    `_ensure_chromium_installed`. Default (no flag) must NOT."""

    def test_need_browser_true_calls_ensure_chromium(self):
        from tests.functional.test_e2e_install_use import _skip_if_missing_prereqs

        with patch(
            "tests.functional.test_e2e_install_use.shutil.which",
            return_value="/usr/bin/exists",
        ), patch(
            "tests.functional.test_e2e_install_use.os.environ.get",
            return_value="set",
        ), patch(
            "tests.functional.test_e2e_install_use._network_reachable",
            return_value=True,
        ), patch(
            "tests.functional.test_e2e_install_use._ensure_chromium_installed",
        ) as ensure:
            _skip_if_missing_prereqs(need_browser=True)

        assert ensure.call_count == 1

    def test_need_browser_default_does_not_call_ensure_chromium(self):
        from tests.functional.test_e2e_install_use import _skip_if_missing_prereqs

        with patch(
            "tests.functional.test_e2e_install_use.shutil.which",
            return_value="/usr/bin/exists",
        ), patch(
            "tests.functional.test_e2e_install_use.os.environ.get",
            return_value="set",
        ), patch(
            "tests.functional.test_e2e_install_use._network_reachable",
            return_value=True,
        ), patch(
            "tests.functional.test_e2e_install_use._ensure_chromium_installed",
        ) as ensure:
            _skip_if_missing_prereqs()

        assert ensure.call_count == 0
