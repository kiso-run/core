"""E2E extended tests — real MCP install + real use, end-to-end.

Replaces the F17/F36 coverage retired by M1633. Those tests pretended
to install via the wrapper subsystem but used a permanently-False
`tool_installed()` helper, so they had been silently dead since v0.10.

These tests do the thing for real: they invoke `kiso mcp install
--from-url <url>` against a known stable upstream from the default
preset (see `docs/default-preset.md`), wait for install to complete,
then drive a conversation where the planner sees the new capability
and uses it. They live in the `extended` tier (>3min, real network,
real subprocess) and gate cleanly on missing prerequisites.

The conversational `use` step uses the `run_message_e2e` fixture
(in conftest), which reloads `config.toml` and reconstructs the MCP
manager before every message — so the second turn sees the
`[mcp.<name>]` block written by the first turn's install.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess

import pytest

from tests.conftest import LLM_MULTI_PLAN_TIMEOUT
from tests.functional.conftest import assert_no_command_word

pytestmark = [pytest.mark.functional, pytest.mark.extended]


# Stable upstream from the default preset. `@playwright/mcp` is
# Microsoft-maintained, requires no API key, and ships in the kiso
# default preset — see docs/default-preset.md.
_BROWSER_MCP_URL = "npm:@playwright/mcp"
_BROWSER_MCP_NAME = "browser"

# Per-test MCP install timeout: real install downloads npm packages
# and the Playwright Chromium browser (~150MB on first run).
_INSTALL_SUBPROCESS_TIMEOUT = 600  # 10 minutes


def _network_reachable(host: str = "registry.npmjs.org", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except (OSError, socket.timeout):
        return False


def _skip_if_missing_prereqs() -> None:
    if not shutil.which("npx"):
        pytest.skip("npx not on PATH — needed to install npm-based MCP servers")
    if not shutil.which("kiso"):
        pytest.skip("kiso CLI not on PATH — install kiso in the test env first")
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — required for live LLM calls")
    if not _network_reachable():
        pytest.skip("npm registry unreachable — extended install needs network")


def _mcp_already_installed(name: str) -> bool:
    """Return True when `kiso mcp list` reports *name* as installed.

    The KISO_HOME env var is set by `_func_kiso_dir` so this query
    sees the isolated test KISO_DIR, not the host's `~/.kiso`.
    """
    try:
        out = subprocess.run(
            ["kiso", "mcp", "list"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ},
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return name in out.stdout


def _install_via_cli(url: str, name: str) -> None:
    """Run `kiso mcp install --from-url <url> --yes` synchronously.

    The `--yes` flag skips the untrusted-source approval prompt, which
    is appropriate inside an isolated extended test.
    """
    result = subprocess.run(
        ["kiso", "mcp", "install", "--from-url", url, "--yes"],
        capture_output=True, text=True,
        timeout=_INSTALL_SUBPROCESS_TIMEOUT,
        env={**os.environ},
    )
    if result.returncode != 0:
        pytest.fail(
            f"`kiso mcp install --from-url {url}` failed "
            f"(exit {result.returncode}). stdout: {result.stdout[-1500:]} "
            f"stderr: {result.stderr[-1500:]}"
        )
    # Sanity: the server should now appear in `kiso mcp list`. If it
    # does not, install partially succeeded — the test cannot proceed.
    if not _mcp_already_installed(name):
        pytest.fail(
            f"`kiso mcp install` reported success but `{name}` is not "
            f"in `kiso mcp list`. stdout: {result.stdout[-800:]}"
        )


# ---------------------------------------------------------------------------
# E2E-1 — Browser MCP install + use (F36-equivalent)
# ---------------------------------------------------------------------------


class TestE2EBrowserInstallAndUse:
    """Install @playwright/mcp via the kiso CLI, then drive the planner
    through a navigate task that must route to the newly-installed MCP.

    Why this exists: the previous TestF36CrossPlanFileHandoff was
    deleted in M1633 because it depended on the retired wrapper
    subsystem. This restores end-to-end signal on the modern
    install-via-MCP path with a real network install + real MCP
    invocation."""

    async def test_browser_mcp_install_then_navigate(
        self, run_message_e2e,
    ):
        """What: real install of browser MCP, then ask the planner to
        navigate to a URL.

        Why: the canonical "install a capability the user just asked
        for, then immediately use it" flow. F36 used to cover this
        via the wrapper subsystem; this restores coverage on the
        v0.12 MCP install path.

        Expects: `kiso mcp list` shows browser installed; the second
        message produces a plan whose tasks include an `mcp` call
        targeting the playwright/browser server; no inline `curl` /
        `wget` rewrite of the navigate intent.
        """
        _skip_if_missing_prereqs()

        # Pre-install via subprocess CLI rather than driving it
        # through the planner. The conversational install flow is
        # already covered by tests/live/test_install_lifecycle.py;
        # here we want to exercise the post-install USE flow, which
        # the prior dead F36 was supposed to cover.
        if not _mcp_already_installed(_BROWSER_MCP_NAME):
            _install_via_cli(_BROWSER_MCP_URL, _BROWSER_MCP_NAME)
        assert _mcp_already_installed(_BROWSER_MCP_NAME), (
            f"Pre-condition failed: {_BROWSER_MCP_NAME} not installed"
        )

        # Drive the planner through a navigate intent. The fresh-config
        # reload in run_message_e2e picks up the new [mcp.browser]
        # block; the planner sees the navigate-capable MCP and must
        # route through it (M1609 invariant).
        result = await run_message_e2e(
            "naviga a https://example.com e dimmi il titolo della pagina",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert result.success, (
            f"Plan failed. Plans: "
            f"{[p.get('status') for p in result.plans]}"
        )

        # The planner must use the installed MCP, not reimplement
        # navigate inline. We accept either the literal `browser`
        # server name or any name containing `playwright` —
        # depending on how the install resolver registered it.
        mcp_calls = [
            t for t in result.tasks
            if t.get("type") == "mcp"
            and (
                t.get("server") == _BROWSER_MCP_NAME
                or "playwright" in (t.get("server") or "").lower()
                or "browser" in (t.get("server") or "").lower()
            )
        ]
        assert mcp_calls, (
            f"Plan did not use the installed browser MCP. "
            f"Task types: {result.task_types()}, "
            f"servers: "
            f"{[t.get('server') for t in result.tasks if t.get('type') == 'mcp']}"
        )
        # M1609 invariant: when an MCP exists for the intent, do not
        # bypass it via inline curl / wget. Apply the word-boundary
        # check on `command` to avoid false positives from page-content
        # text that may contain "curl" / "wget" as substrings.
        assert_no_command_word(result.tasks, ["curl", "wget"])
