"""L6 — Plugin tests.

Clone each official skill/connector from the registry, install deps, and
run their internal test suite.

Gated behind ``--live-network`` flag (requires git + network access).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_network

OFFICIAL_ORG = "kiso-run"


def _clone_and_test(tmp_path: Path, kind: str, name: str) -> None:
    """Clone an official plugin repo, sync deps, and run its tests."""
    prefix = "skill-" if kind == "skill" else "connector-"
    git_url = f"https://github.com/{OFFICIAL_ORG}/{prefix}{name}.git"
    plugin_dir = tmp_path / name

    # Clone
    result = subprocess.run(
        ["git", "clone", "--depth", "1", git_url, str(plugin_dir)],
        capture_output=True, text=True,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": subprocess.os.environ.get("PATH", "")},
    )
    if result.returncode != 0:
        pytest.skip(f"{prefix}{name} repo not available: {result.stderr.strip()}")

    # Sync deps (including dev group for tests)
    result = subprocess.run(
        ["uv", "sync", "--group", "dev"],
        cwd=str(plugin_dir),
        capture_output=True, text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"uv sync failed for {prefix}{name}: {result.stderr.strip()}")

    # Check if tests exist
    tests_dir = plugin_dir / "tests"
    if not tests_dir.is_dir() or not list(tests_dir.glob("test_*.py")):
        pytest.skip(f"{prefix}{name} has no tests")

    # Run tests
    result = subprocess.run(
        ["uv", "run", "--group", "dev", "pytest", "tests/", "-v", "--tb=short"],
        cwd=str(plugin_dir),
        capture_output=True, text=True,
        timeout=120,
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
# L6.1 — Skill plugin tests
# ---------------------------------------------------------------------------


class TestSkillPlugins:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.registry = _load_registry()

    def test_skill_plugins(self, tmp_path: Path):
        """Clone and test each official skill from the registry."""
        skills = self.registry.get("skills", [])
        if not skills:
            pytest.skip("No skills in registry")

        for skill in skills:
            _clone_and_test(tmp_path, "skill", skill["name"])


# ---------------------------------------------------------------------------
# L6.2 — Connector plugin tests
# ---------------------------------------------------------------------------


class TestConnectorPlugins:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.registry = _load_registry()

    def test_connector_plugins(self, tmp_path: Path):
        """Clone and test each official connector from the registry."""
        connectors = self.registry.get("connectors", [])
        if not connectors:
            pytest.skip("No connectors in registry")

        for connector in connectors:
            _clone_and_test(tmp_path, "connector", connector["name"])
