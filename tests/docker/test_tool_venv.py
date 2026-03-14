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
        """What: Checks that check_deps finds a binary located in the tool's .venv/bin/.

        Why: Validates venv-aware dependency checking — pip-installed CLIs must be discoverable.
        Expects: Empty missing list (binary found).
        """
        missing = check_deps(tool_with_venv_binary)
        assert missing == []

    def test_missing_binary_reported(self, tool_with_venv_binary):
        """What: Checks that check_deps reports a binary not present in venv or system PATH.

        Why: Validates that truly missing dependencies are correctly detected.
        Expects: Missing list contains the nonexistent binary name.
        """
        tool_with_venv_binary["deps"]["bin"] = ["nonexistent-xyz-binary"]
        missing = check_deps(tool_with_venv_binary)
        assert "nonexistent-xyz-binary" in missing

    def test_mixed_found_and_missing(self, tool_with_venv_binary):
        """What: Checks deps with one binary in venv and one nonexistent.

        Why: Validates check_deps correctly partitions found vs missing binaries.
        Expects: Only the nonexistent binary appears in the missing list.
        """
        tool_with_venv_binary["deps"]["bin"] = ["fake-tool", "nonexistent-xyz"]
        missing = check_deps(tool_with_venv_binary)
        assert missing == ["nonexistent-xyz"]


class TestBuildToolEnvVenv:
    """Verify build_tool_env includes .venv/bin/ in PATH."""

    def test_venv_bin_in_path(self, tool_with_venv_binary):
        """What: Checks that build_tool_env prepends .venv/bin/ to the PATH.

        Why: Validates that venv binaries take precedence over system binaries in the tool environment.
        Expects: PATH starts with the tool's .venv/bin/ directory.
        """
        env = build_tool_env(tool_with_venv_binary)
        venv_bin = str(Path(tool_with_venv_binary["path"]) / ".venv" / "bin")
        assert env["PATH"].startswith(venv_bin)

    def test_system_path_preserved(self, tool_with_venv_binary):
        """What: Checks that system PATH entries are preserved after the venv bin prefix.

        Why: Validates that adding the venv to PATH does not clobber system-wide binaries.
        Expects: PATH has at least 2 entries (venv + system).
        """
        env = build_tool_env(tool_with_venv_binary)
        path_parts = env["PATH"].split(":")
        assert len(path_parts) >= 2

    def test_no_venv_without_tool_path(self):
        """What: Calls build_tool_env for a tool with an empty path string.

        Why: Validates graceful fallback — tools without a path get system PATH without a bogus venv prefix.
        Expects: PATH is non-empty and does not start with '/.venv/bin'.
        """
        tool = {"name": "no-path", "path": "", "deps": {}, "env": {}}
        env = build_tool_env(tool)
        # PATH should not start with a tool venv prefix
        assert not env["PATH"].startswith("/.venv/bin")
        assert env["PATH"]  # system PATH still present
