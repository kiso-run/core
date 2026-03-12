"""Docker integration tests for tool venv binary detection.

Verifies that check_deps() and build_tool_env() correctly find
pip-installed CLIs inside a tool's .venv/bin/ directory.

Run inside the dev container:
    docker compose -f docker-compose.test.yml --profile docker run --rm test-docker
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.tools import build_tool_env, check_deps


@pytest.fixture()
def tool_with_venv_binary(tmp_path):
    """Create a minimal tool dir with a fake binary in .venv/bin/."""
    tool_dir = tmp_path / "test-tool"
    venv_bin = tool_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    fake_bin = venv_bin / "fake-tool"
    fake_bin.write_text("#!/bin/sh\necho ok\n")
    fake_bin.chmod(0o755)

    return {
        "name": "test-tool",
        "path": str(tool_dir),
        "deps": {"bin": ["fake-tool"]},
        "env": {},
        "summary": "test tool",
    }


class TestCheckDepsVenv:
    def test_venv_binary_found(self, tool_with_venv_binary):
        """check_deps finds binary in .venv/bin/ even if not on system PATH."""
        missing = check_deps(tool_with_venv_binary)
        assert missing == []

    def test_missing_binary_reported(self, tool_with_venv_binary):
        """Binary not in venv or system PATH is reported as missing."""
        tool_with_venv_binary["deps"]["bin"] = ["nonexistent-xyz-binary"]
        missing = check_deps(tool_with_venv_binary)
        assert "nonexistent-xyz-binary" in missing

    def test_mixed_found_and_missing(self, tool_with_venv_binary):
        """Some binaries found in venv, others missing."""
        tool_with_venv_binary["deps"]["bin"] = ["fake-tool", "nonexistent-xyz"]
        missing = check_deps(tool_with_venv_binary)
        assert missing == ["nonexistent-xyz"]


class TestBuildToolEnvVenv:
    """Verify build_tool_env includes .venv/bin/ in PATH."""

    def test_venv_bin_in_path(self, tool_with_venv_binary):
        """build_tool_env includes .venv/bin/ at the start of PATH."""
        env = build_tool_env(tool_with_venv_binary)
        venv_bin = str(Path(tool_with_venv_binary["path"]) / ".venv" / "bin")
        assert env["PATH"].startswith(venv_bin)

    def test_system_path_preserved(self, tool_with_venv_binary):
        """System PATH entries are preserved after venv bin."""
        env = build_tool_env(tool_with_venv_binary)
        path_parts = env["PATH"].split(":")
        assert len(path_parts) >= 2

    def test_no_venv_without_tool_path(self):
        """Tool without path gets system PATH only."""
        tool = {"name": "no-path", "path": "", "deps": {}, "env": {}}
        env = build_tool_env(tool)
        # PATH should not start with a tool venv prefix
        assert not env["PATH"].startswith("/.venv/bin")
        assert env["PATH"]  # system PATH still present
