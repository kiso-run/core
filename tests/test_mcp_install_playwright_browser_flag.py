"""Regression tests for M1642.

`@playwright/mcp` defaults to launching the `chrome` channel
(system Chrome). On hosts without system Chrome installed
(every fresh runner) every browser action fails with `Chromium
distribution 'chrome' is not found at /opt/google/chrome/chrome`.

To use the Playwright-bundled Chromium (installed by
`npx playwright install chromium`) the MCP needs
`--browser=chromium` as a runtime arg. This must happen for both:

1. The `@playwright/mcp` install via `--from-url npm:@playwright/mcp`
   (resolver path), regardless of version pin.
2. The default preset's `browser` entry (preset path).

Other npm packages must NOT receive the flag (regression guard).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiso.mcp.install import _resolve_npm_pkg


class TestPlaywrightMcpHeuristic:
    """`_resolve_npm_pkg` must append `--browser=chromium` when the
    package is `@playwright/mcp`. Version pins are not currently
    supported through the from-url npm install regex (separate gap),
    so only the unpinned form is exercised here."""

    def test_unpinned_playwright_mcp_gets_chromium_flag(self):
        res = _resolve_npm_pkg("@playwright/mcp", None)
        assert res.args == ["-y", "@playwright/mcp", "--browser=chromium"]

    def test_other_npm_packages_unchanged(self):
        """Regression guard: heuristic fires only for playwright."""
        res = _resolve_npm_pkg("@modelcontextprotocol/server-filesystem", None)
        assert res.args == ["-y", "@modelcontextprotocol/server-filesystem"]
        assert "--browser=chromium" not in res.args

    def test_unscoped_npm_package_unchanged(self):
        res = _resolve_npm_pkg("some-mcp", None)
        assert res.args == ["-y", "some-mcp"]
        assert "--browser=chromium" not in res.args


class TestDefaultPresetUsesChromium:
    """The shipped preset's `browser` entry must include
    `--browser=chromium` so `kiso init --preset default` works on
    hosts without system Chrome."""

    def test_browser_args_include_chromium_flag(self):
        preset_path = (
            Path(__file__).resolve().parents[1]
            / "kiso" / "presets" / "default.mcp.json"
        )
        data = json.loads(preset_path.read_text())
        browser_args = data["mcpServers"]["browser"]["args"]
        assert "--browser=chromium" in browser_args, (
            f"default preset browser args must include "
            f"--browser=chromium; got: {browser_args}"
        )
