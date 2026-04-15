"""Tests for the planner's `npx -y` routing rule.

Mirrors the existing `pip install` → `uv pip install` rewrite
(see `_PIP_INSTALL_RE` / `_UV_PIP_RE` and
`kiso/brain/planner.py:237`) but for the Node ecosystem:

- `_NPM_GLOBAL_RE` matches `npm install -g`, `npm i -g`, and
  `npm install --global` invocations.
- `_NPX_RE` is the "already correct" detector that suppresses
  the validator error when the same exec detail also references
  `npx`.
- `validate_plan` rejects an exec task whose detail contains a
  global npm install without a corresponding `npx` form.
- `_classify_install_mode` returns the new `node_cli` mode when
  the user explicitly asks to install something with a Node
  signal (npm/npx/node package/...).
- `_build_install_mode_context` formats the Node CLI route as
  ``Route: Node CLI tool — exec `npx -y <target>` ``.

Why npx over npm install -g: `npx -y <pkg>` runs the package
ephemerally in the npm cache, never polluting global state, and
matches how the v0.10 default preset will invoke MCP servers
distributed via npm. Global installs are appropriate only for
long-running development tools, which is not the kiso runtime
use case.
"""

from __future__ import annotations

import pytest

from kiso.brain.common import (
    _NPM_GLOBAL_RE,
    _NPX_RE,
    _build_install_mode_context,
    _classify_install_mode,
)
from kiso.brain.planner import validate_plan


# ---------------------------------------------------------------------------
# Regex contract
# ---------------------------------------------------------------------------


class TestNpmGlobalRegex:
    """`_NPM_GLOBAL_RE` matches the four shapes a model produces."""

    @pytest.mark.parametrize(
        "command",
        [
            "npm install -g typescript",
            "npm i -g eslint",
            "npm install --global @playwright/mcp",
            "sudo npm install -g prettier",
        ],
    )
    def test_matches_global_install_shapes(self, command: str) -> None:
        assert _NPM_GLOBAL_RE.search(command), (
            f"_NPM_GLOBAL_RE must match: {command!r}"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "npm install lodash",  # local install, not global
            "npm ci",
            "npm test",
            "npx -y @playwright/mcp@latest",
            "yarn global add typescript",  # different package manager
        ],
    )
    def test_does_not_match_non_global_or_unrelated(self, command: str) -> None:
        assert not _NPM_GLOBAL_RE.search(command), (
            f"_NPM_GLOBAL_RE must NOT match: {command!r}"
        )


class TestNpxRegex:
    """`_NPX_RE` recognises the 'already correct' form."""

    @pytest.mark.parametrize(
        "command",
        [
            "npx -y @modelcontextprotocol/server-filesystem /workspace",
            "npx --yes typescript",
            "npx prettier --write .",
        ],
    )
    def test_matches_npx_invocations(self, command: str) -> None:
        assert _NPX_RE.search(command)

    @pytest.mark.parametrize(
        "command",
        [
            "npm install -g typescript",
            "npm test",
            "node script.js",
        ],
    )
    def test_does_not_match_non_npx(self, command: str) -> None:
        assert not _NPX_RE.search(command)


# ---------------------------------------------------------------------------
# validate_plan integration
# ---------------------------------------------------------------------------


def _exec_plan(detail: str) -> dict:
    return {
        "tasks": [
            {
                "type": "exec",
                "detail": detail,
                "expect": "ok",
                "wrapper": None,
                "args": None,
            }
        ]
    }


class TestValidatePlanRejectsGlobalNpm:
    """`validate_plan` enforces npx over npm install -g."""

    def test_rejects_npm_install_global(self) -> None:
        plan = _exec_plan("Run npm install -g typescript to set up the toolchain")
        errors = validate_plan(plan)
        assert any(
            "npx -y" in e and "npm install -g" in e for e in errors
        ), errors

    def test_rejects_npm_i_global(self) -> None:
        plan = _exec_plan("Use npm i -g eslint and then lint the project")
        errors = validate_plan(plan)
        assert any("npx -y" in e for e in errors), errors

    def test_accepts_npx_invocation(self) -> None:
        plan = _exec_plan("Run npx -y @modelcontextprotocol/server-filesystem")
        errors = validate_plan(plan)
        assert not any("npx" in e and "npm install -g" in e for e in errors), errors

    def test_accepts_local_npm_install(self) -> None:
        plan = _exec_plan("Run npm install in the project directory to fetch deps")
        errors = validate_plan(plan)
        assert not any(
            "npx -y" in e and "npm install -g" in e for e in errors
        ), errors

    def test_accepts_unrelated_exec(self) -> None:
        plan = _exec_plan("List files in the workspace with ls -la")
        errors = validate_plan(plan)
        assert not any("npx" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Install router
# ---------------------------------------------------------------------------


class TestInstallRouterNodeCli:
    """`_classify_install_mode` routes Node-flavoured installs to npx."""

    def test_explicit_npm_signal_routes_node_cli(self) -> None:
        route = _classify_install_mode(
            "install typescript using npm",
            {
                "os": {"pkg_manager": "apt"},
                "available_binaries": ["python3", "node", "npm", "npx"],
            },
            installed_wrapper_names=[],
            registry_hint_names={"browser", "aider"},
        )
        assert route["mode"] == "node_cli"
        assert route["target"] == "typescript"

    def test_explicit_npx_signal_routes_node_cli(self) -> None:
        route = _classify_install_mode(
            "install prettier via npx",
            {
                "os": {"pkg_manager": "apt"},
                "available_binaries": ["python3", "node", "npm", "npx"],
            },
            installed_wrapper_names=[],
            registry_hint_names={"browser", "aider"},
        )
        assert route["mode"] == "node_cli"
        assert route["target"] == "prettier"

    def test_python_signal_still_routes_python_lib(self) -> None:
        """Make sure the Node branch does not steal Python requests."""
        route = _classify_install_mode(
            "install flask using pip",
            {
                "os": {"pkg_manager": "apt"},
                "available_binaries": ["python3", "node", "npm", "uv"],
            },
            installed_wrapper_names=[],
            registry_hint_names={"browser", "aider"},
        )
        assert route["mode"] == "python_lib"
        assert route["target"] == "flask"


class TestInstallContextNodeCli:
    """`_build_install_mode_context` formats the Node CLI route."""

    def test_node_cli_context_format(self) -> None:
        text = _build_install_mode_context(
            {
                "mode": "node_cli",
                "target": "playwright",
                "reason": "user explicitly referenced Node CLI tooling",
            },
            {"os": {"pkg_manager": "apt"}},
        )
        assert "Mode: node_cli" in text
        assert "npx -y playwright" in text
        assert "Do not use" in text or "Do not set" in text

    def test_node_cli_context_recommends_npx_not_pip(self) -> None:
        """The Route line itself must point to npx, not uv pip.

        The warning paragraph may legitimately mention 'uv pip install'
        as a thing to NOT do — what matters is that the actionable
        Route: line tells the planner to use npx.
        """
        text = _build_install_mode_context(
            {"mode": "node_cli", "target": "typescript", "reason": "any"},
            {"os": {"pkg_manager": "apt"}},
        )
        route_lines = [ln for ln in text.splitlines() if ln.startswith("Route:")]
        assert len(route_lines) == 1
        assert "npx -y typescript" in route_lines[0]
        assert "uv pip install" not in route_lines[0]
        assert "apt-get" not in route_lines[0]
