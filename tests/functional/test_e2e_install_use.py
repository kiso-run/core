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
from pathlib import Path

import pytest

from tests.conftest import LLM_MULTI_PLAN_TIMEOUT
from tests.functional.conftest import assert_no_command_word

pytestmark = [pytest.mark.functional, pytest.mark.extended]


# Stable upstreams from the default preset. `@playwright/mcp` is
# Microsoft-maintained, no API key. `kiso-run/ocr-mcp` ships in the
# kiso default preset and uses Gemini via OpenRouter, so it needs
# OPENROUTER_API_KEY (already a global skip-condition).
_BROWSER_MCP_URL = "npm:@playwright/mcp"
_BROWSER_MCP_NAME = "browser"

_OCR_MCP_URL = "https://github.com/kiso-run/ocr-mcp"
_OCR_MCP_NAME = "ocr"

# Per-test MCP install timeout: real install downloads npm packages
# and the Playwright Chromium browser (~150MB on first run).
_INSTALL_SUBPROCESS_TIMEOUT = 600  # 10 minutes

# Repo root (the directory containing pyproject.toml). All `uv run
# kiso ...` subprocesses use this as `cwd` so they always pick up
# the project's local CLI via [project.scripts] in the project venv,
# never a `kiso` binary that may exist elsewhere on PATH.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _kiso_cmd(*args: str) -> list[str]:
    """Build a `uv run kiso ...` command list.

    Tests must exercise the CLI from THIS repo, not whatever `kiso`
    binary the host happens to have on PATH. `uv run` resolves the
    `kiso` script from the project's `[project.scripts]` section
    using the project venv, so the binary version always matches the
    code under test. No PATH `kiso` is required.
    """
    return ["uv", "run", "--project", str(_REPO_ROOT), "kiso", *args]


def _network_reachable(host: str = "registry.npmjs.org", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except (OSError, socket.timeout):
        return False


def _skip_if_missing_prereqs(*, need_github: bool = False) -> None:
    """Common skip-conditions for extended E2E install tests.

    `uv` is always required (the tests invoke the kiso CLI via
    `uv run kiso ...` against the project venv — no host-PATH `kiso`
    dependency). `npx` is always required (every extended test
    installs at least one npm-based MCP). `need_github=True` adds a
    reachability check on github.com for tests that also install a
    kiso-run MCP from a github URL.
    """
    if not shutil.which("uv"):
        pytest.skip("uv not on PATH — needed to invoke the project's kiso CLI")
    if not shutil.which("npx"):
        pytest.skip("npx not on PATH — needed to install npm-based MCP servers")
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — required for live LLM calls")
    if not _network_reachable():
        pytest.skip("npm registry unreachable — extended install needs network")
    if need_github and not _network_reachable("github.com", 443):
        pytest.skip("github.com unreachable — kiso-run MCP install needs network")


def _mcp_already_installed(name: str) -> bool:
    """Return True when `kiso mcp list` reports *name* as installed.

    The KISO_HOME env var is set by `_func_kiso_dir` so this query
    sees the isolated test KISO_DIR, not the host's `~/.kiso`.
    """
    try:
        out = subprocess.run(
            _kiso_cmd("mcp", "list"),
            capture_output=True, text=True, timeout=30,
            env={**os.environ}, cwd=str(_REPO_ROOT),
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return name in out.stdout


def _install_via_cli(url: str, name: str) -> None:
    """Run `kiso mcp install --from-url <url> --yes` synchronously
    via `uv run kiso ...` so the CLI matches the project under test.

    The `--yes` flag skips the untrusted-source approval prompt, which
    is appropriate inside an isolated extended test.
    """
    result = subprocess.run(
        _kiso_cmd("mcp", "install", "--from-url", url, "--yes"),
        capture_output=True, text=True,
        timeout=_INSTALL_SUBPROCESS_TIMEOUT,
        env={**os.environ}, cwd=str(_REPO_ROOT),
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


# ---------------------------------------------------------------------------
# E2E-2 — Cross-MCP install + cross-plan handoff (F17-equivalent)
# ---------------------------------------------------------------------------


class TestE2ECrossMCPHandoff:
    """Install browser + OCR MCPs for real, then drive a two-plan
    conversation: plan 1 takes a screenshot, plan 2 OCRs it. Restores
    the F17 multi-MCP coverage on the v0.12 install path.

    Why this exists: the deleted F17 chained four wrapper-era servers
    (browser, ocr, aider, exec). The wrapper subsystem is gone; the
    shape that still has unique signal is the **cross-MCP cross-plan
    handoff** — output from MCP A in plan 1 must reach MCP B in
    plan 2 via the session workspace listing. F40 / M1635 cover
    single-plan multi-step; this is the only test that exercises
    real install of two MCPs and real cross-plan capability routing
    end-to-end."""

    async def test_browser_screenshot_then_ocr_extracts_text(
        self, run_message_e2e,
    ):
        """What: real install of browser + ocr MCPs, plan 1 takes a
        screenshot of example.com, plan 2 OCRs the screenshot from
        plan 1.

        Why: F17 used to validate that file artifacts produced by one
        MCP in plan N are picked up by another MCP in plan N+1 via
        the planner's session-workspace listing. The wrapper subsystem
        retirement made F17 dead code. This restores end-to-end
        signal on the modern MCP install path with real upstreams.

        Expects:
        - Both MCPs present in `kiso mcp list` after pre-install.
        - Plan 1 produces a `.png` artifact in the session workspace.
        - Plan 2 contains an `mcp` task targeting the OCR server,
          referencing the screenshot from plan 1.
        - The final messenger output contains a recognisable token
          from the example.com page ("example", "domain", or
          "illustrative") — proves the OCR call actually happened
          and its result reached the user.
        """
        _skip_if_missing_prereqs(need_github=True)

        # Pre-install both MCPs via subprocess CLI (idempotent).
        if not _mcp_already_installed(_BROWSER_MCP_NAME):
            _install_via_cli(_BROWSER_MCP_URL, _BROWSER_MCP_NAME)
        if not _mcp_already_installed(_OCR_MCP_NAME):
            _install_via_cli(_OCR_MCP_URL, _OCR_MCP_NAME)
        assert _mcp_already_installed(_BROWSER_MCP_NAME), (
            f"Pre-condition failed: {_BROWSER_MCP_NAME} not installed"
        )
        assert _mcp_already_installed(_OCR_MCP_NAME), (
            f"Pre-condition failed: {_OCR_MCP_NAME} not installed"
        )

        # --- Plan 1: screenshot via browser MCP ---
        r1 = await run_message_e2e(
            "naviga a https://example.com e fai uno screenshot della pagina",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r1.success, (
            f"Plan 1 (screenshot) failed. Plans: "
            f"{[p.get('status') for p in r1.plans]}"
        )
        # The browser MCP must have been used, and a .png must have
        # landed in the session workspace (auto-published by kiso).
        browser_calls_p1 = [
            t for t in r1.tasks
            if t.get("type") == "mcp"
            and (
                t.get("server") == _BROWSER_MCP_NAME
                or "playwright" in (t.get("server") or "").lower()
                or "browser" in (t.get("server") or "").lower()
            )
        ]
        assert browser_calls_p1, (
            f"Plan 1 must use the browser MCP. "
            f"Servers seen: "
            f"{[t.get('server') for t in r1.tasks if t.get('type') == 'mcp']}"
        )
        assert r1.has_published_file("*.png") or any(
            ".png" in (t.get("output") or "").lower() for t in r1.tasks
        ), (
            f"Plan 1 did not produce a .png artifact. "
            f"Pub files: {r1.pub_files}"
        )

        # --- Plan 2: OCR the screenshot via OCR MCP, cross-plan ---
        r2 = await run_message_e2e(
            "ora estrai il testo dallo screenshot con OCR e dimmi cosa contiene",
            timeout=LLM_MULTI_PLAN_TIMEOUT,
        )
        assert r2.success, (
            f"Plan 2 (OCR) failed. Plans: "
            f"{[p.get('status') for p in r2.plans]}"
        )
        last_plan_id = r2.plans[-1]["id"]
        ocr_calls_p2 = [
            t for t in r2.tasks
            if t.get("type") == "mcp"
            and t.get("server") == _OCR_MCP_NAME
            and t.get("plan_id") == last_plan_id
        ]
        assert ocr_calls_p2, (
            f"Plan 2 must use the OCR MCP. Servers seen in last plan: "
            f"{[t.get('server') for t in r2.tasks if t.get('type') == 'mcp' and t.get('plan_id') == last_plan_id]}"
        )

        # Cross-plan handoff: plan 2's OCR call must reference the
        # screenshot file produced in plan 1. We accept any task in
        # plan 2 whose detail / args / command mention `.png` or
        # `screenshot`, since the planner has freedom in how it
        # phrases the cross-plan reference.
        last_plan_tasks = [
            t for t in r2.tasks if t.get("plan_id") == last_plan_id
        ]
        cross_plan_blob = "\n".join(
            ((t.get("detail") or "")
             + "\n" + (t.get("command") or "")
             + "\n" + str(t.get("args") or ""))
            for t in last_plan_tasks
        ).lower()
        assert ".png" in cross_plan_blob or "screenshot" in cross_plan_blob, (
            f"Plan 2 did not reference the screenshot from plan 1. "
            f"Last-plan task blob: {cross_plan_blob[:600]}"
        )

        # The final messenger output must contain a recognisable
        # token from the example.com OCR result. The page contains
        # "Example Domain" + "This domain is for use in illustrative
        # examples" — multiple spelling-tolerant tokens give
        # robustness against minor OCR noise.
        msg_output = r2.last_plan_msg_output.lower()
        assert any(
            tok in msg_output
            for tok in ("example", "domain", "illustrative", "esempio", "dominio")
        ), (
            f"Plan 2 messenger output does not contain example.com OCR "
            f"tokens. Output: {r2.last_plan_msg_output[:500]}"
        )

        # Defensive: neither plan should have re-implemented browser /
        # OCR via inline shell commands.
        assert_no_command_word(r1.tasks, ["curl", "wget"])
        assert_no_command_word(r2.tasks, ["curl", "wget", "tesseract"])
