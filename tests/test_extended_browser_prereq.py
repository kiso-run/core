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

    def test_runs_playwright_mcp_install_browser_command_non_root(self):
        """Non-root: only the browser-binary install is attempted.
        System deps install requires apt-get (root); skip silently
        when not root, the binary install is enough on developer
        hosts that already have the libs."""
        from tests.functional.test_e2e_install_use import _ensure_chromium_installed

        run_mock = _make_run_mock(returncode=0)
        with patch(
            "tests.functional.test_e2e_install_use.os.geteuid",
            return_value=1000,  # non-root
        ), patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            return_value=run_mock,
        ) as run:
            _ensure_chromium_installed()

        assert run.call_count == 1, (
            "non-root: expected only the install-browser call"
        )
        argv = run.call_args.args[0]
        assert argv[0] == "npx"
        assert "@playwright/mcp" in argv
        assert "install-browser" in argv
        assert "chrome-for-testing" in argv

    def test_skips_test_when_browser_install_fails(self):
        from tests.functional.test_e2e_install_use import _ensure_chromium_installed

        run_mock = _make_run_mock(returncode=1, stderr="network error")
        with patch(
            "tests.functional.test_e2e_install_use.os.geteuid",
            return_value=1000,
        ), patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            return_value=run_mock,
        ):
            with pytest.raises(pytest.skip.Exception):
                _ensure_chromium_installed()


class TestEnsureChromiumInstalledRoot:
    """M1653: when running as root, also install Playwright system
    deps (libnss3/libxss1/libgbm1/...) via `npx playwright install-deps
    chrome-for-testing`. Without these libs, chrome-for-testing
    launches and immediately crashes — surfaced by the user's
    container run on 2026-05-09."""

    def test_root_runs_install_browser_and_install_deps(self):
        from tests.functional.test_e2e_install_use import _ensure_chromium_installed

        # Two subprocess.run calls in order:
        # 1. npx @playwright/mcp install-browser chrome-for-testing
        # 2. npx playwright install-deps chrome-for-testing
        with patch(
            "tests.functional.test_e2e_install_use.os.geteuid",
            return_value=0,  # root
        ), patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            side_effect=[
                _make_run_mock(returncode=0),  # install-browser
                _make_run_mock(returncode=0),  # install-deps
            ],
        ) as run:
            _ensure_chromium_installed()

        assert run.call_count == 2
        deps_argv = run.call_args_list[1].args[0]
        assert deps_argv[0] == "npx"
        assert "playwright" in deps_argv
        assert "install-deps" in deps_argv
        assert "chrome-for-testing" in deps_argv

    def test_root_skip_with_diagnostic_when_install_deps_fails(self):
        """install-deps failure surfaces apt-get / network error in the
        skip reason — not a generic 'unreachable'."""
        from tests.functional.test_e2e_install_use import _ensure_chromium_installed

        with patch(
            "tests.functional.test_e2e_install_use.os.geteuid",
            return_value=0,
        ), patch(
            "tests.functional.test_e2e_install_use.subprocess.run",
            side_effect=[
                _make_run_mock(returncode=0),  # install-browser ok
                _make_run_mock(
                    returncode=1,
                    stderr="E: Unable to locate package libnss3-dev",
                ),
            ],
        ):
            with pytest.raises(pytest.skip.Exception) as exc_info:
                _ensure_chromium_installed()
            # Skip reason must surface the apt-get error tail
            assert "libnss3" in str(exc_info.value)


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
