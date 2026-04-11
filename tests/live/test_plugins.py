"""L6 — Plugin tests.

Clone each official skill/connector from the registry, install deps, and
run their internal test suite.

Gated behind ``--live-network`` flag (requires git + network access).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_network

OFFICIAL_ORG = "kiso-run"


def _clean_subprocess_env() -> dict[str, str]:
    """Build a minimal env for plugin subprocess calls.

    Prevents host secrets (KISO_LLM_API_KEY, KISO_WRAPPER_* etc.) from leaking
    into plugin test suites, which could mask missing-key test assertions.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    # uv needs its cache dir to avoid re-downloading packages
    for key in ("UV_CACHE_DIR", "XDG_CACHE_HOME"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _clone_and_test(tmp_path: Path, kind: str, name: str) -> None:
    """Clone an official plugin repo, sync deps, and run its tests."""
    prefix = "wrapper-" if kind == "wrapper" else "connector-"
    git_url = f"https://github.com/{OFFICIAL_ORG}/{prefix}{name}.git"
    plugin_dir = tmp_path / name

    # Clone — explicit minimal env (no secrets)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", git_url, str(plugin_dir)],
        capture_output=True, text=True,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": os.environ.get("PATH", "")},
    )
    if result.returncode != 0:
        pytest.skip(f"{prefix}{name} repo not available: {result.stderr.strip()}")

    clean_env = _clean_subprocess_env()

    # Sync deps (including dev group for tests)
    result = subprocess.run(
        ["uv", "sync", "--group", "dev"],
        cwd=str(plugin_dir),
        capture_output=True, text=True,
        timeout=120,
        env=clean_env,
    )
    if result.returncode != 0:
        pytest.fail(f"uv sync failed for {prefix}{name}: {result.stderr.strip()}")

    # Check if tests exist
    tests_dir = plugin_dir / "tests"
    if not tests_dir.is_dir() or not list(tests_dir.glob("test_*.py")):
        pytest.skip(f"{prefix}{name} has no tests")

    # Run tests — isolated env prevents host secrets from leaking
    result = subprocess.run(
        ["uv", "run", "--group", "dev", "pytest", "tests/", "-v", "--tb=short"],
        cwd=str(plugin_dir),
        capture_output=True, text=True,
        timeout=120,
        env=clean_env,
    )
    if result.returncode != 0:
        pytest.fail(
            f"{prefix}{name} tests failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


def _load_registry() -> dict:
    """Load registry.json from the repo root."""
    registry_path = Path(__file__).resolve().parents[2] / "registry.json"
    return json.loads(registry_path.read_text())


# ---------------------------------------------------------------------------
# L6.1 — Tool plugin tests
# ---------------------------------------------------------------------------


class TestToolPlugins:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.registry = _load_registry()

    def test_tool_plugins(self, tmp_path: Path):
        """What: Clones each official tool plugin from the registry, installs deps, runs its test suite.

        Why: Validates that all official tool plugins build and pass their own tests.
        Expects: All plugin tests pass (non-zero exit causes failure).
        """
        tools = self.registry.get("tools", [])
        if not tools:
            pytest.skip("No tools in registry")

        for tool in tools:
            _clone_and_test(tmp_path, "tool", tool["name"])


# ---------------------------------------------------------------------------
# L6.2 — Connector plugin tests
# ---------------------------------------------------------------------------


class TestConnectorPlugins:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.registry = _load_registry()

    def test_connector_plugins(self, tmp_path: Path):
        """What: Clones each official connector plugin from the registry, installs deps, runs its test suite.

        Why: Validates that all official connector plugins build and pass their own tests.
        Expects: All connector tests pass (non-zero exit causes failure).
        """
        connectors = self.registry.get("connectors", [])
        if not connectors:
            pytest.skip("No connectors in registry")

        for connector in connectors:
            _clone_and_test(tmp_path, "connector", connector["name"])


# ---------------------------------------------------------------------------
# L6.3 — Registry integrity: all entries have existing repos
# ---------------------------------------------------------------------------


class TestRegistryIntegrity:
    """Verify every entry in registry.json has a clonable GitHub repo."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.registry = _load_registry()

    def _check_repo_exists(self, prefix: str, name: str) -> None:
        """Assert that the GitHub repo exists via git ls-remote."""
        git_url = f"https://github.com/{OFFICIAL_ORG}/{prefix}{name}.git"
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", git_url, "HEAD"],
            capture_output=True, text=True, timeout=15,
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": subprocess.os.environ.get("PATH", "")},
        )
        assert result.returncode == 0, (
            f"Registry lists '{prefix}{name}' but repo {git_url} does not exist "
            f"or is not accessible: {result.stderr.strip()}"
        )

    def test_all_tools_exist(self):
        """Every tool in registry.json has a GitHub repo."""
        for tool in self.registry.get("tools", []):
            self._check_repo_exists("tool-", tool["name"])

    def test_all_connectors_exist(self):
        """Every connector in registry.json has a GitHub repo."""
        for conn in self.registry.get("connectors", []):
            self._check_repo_exists("connector-", conn["name"])

    def test_all_presets_exist(self):
        """Every preset in registry.json has a GitHub repo."""
        for preset in self.registry.get("presets", []):
            self._check_repo_exists("preset-", preset["name"])
