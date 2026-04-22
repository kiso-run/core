"""Tests for per-server ``~/.kiso/mcp/<name>.env`` file integration.

Business requirement: ``kiso mcp env set <name> KEY VAL`` writes a
credential to ``~/.kiso/mcp/<name>.env`` with mode 0600. Until this
milestone that file existed on disk but was never read by the
subprocess spawner, so setting a credential had no runtime effect
— a silent regression hazard.

Contract (for ``MCPStdioClient._build_env``):
- If ``~/.kiso/mcp/<name>.env`` exists and is mode 0600, parse
  ``KEY=VAL`` lines (ignore blank + ``#`` comments) and merge
  into the subprocess env **after** the server's config ``env``
  block, so the file wins over the TOML-declared env.
- If the file is present but mode is not 0600, log a warning and
  skip the file (fail closed — never load a world-readable creds
  file).
- The file must not set ``KISO_*`` keys (those are reserved); any
  such line is silently dropped with a warning.
- Missing file → no-op.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.stdio import MCPStdioClient


def _server(name: str = "s1", env: dict | None = None) -> MCPServer:
    return MCPServer(
        name=name,
        transport="stdio",
        command="dummy",
        env=env or {},
    )


def _write_env_file(path: Path, body: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(path, mode)


@pytest.fixture
def mcp_env_dir(tmp_path, monkeypatch):
    """Point ``KISO_DIR`` at tmp_path so the spawner looks under it."""
    monkeypatch.setattr("kiso.config.KISO_DIR", tmp_path)
    return tmp_path / "mcp"


class TestEnvFileMerge:
    def test_missing_file_is_noop(self, mcp_env_dir):
        client = MCPStdioClient(_server("s1"))
        env = client._build_env()
        assert "GITHUB_TOKEN" not in env

    def test_present_file_merges_values(self, mcp_env_dir):
        _write_env_file(mcp_env_dir / "s1.env", "GITHUB_TOKEN=ghp_abc\n")
        client = MCPStdioClient(_server("s1"))
        env = client._build_env()
        assert env["GITHUB_TOKEN"] == "ghp_abc"

    def test_file_wins_over_config_env(self, mcp_env_dir):
        _write_env_file(mcp_env_dir / "s1.env", "GITHUB_TOKEN=from-file\n")
        client = MCPStdioClient(
            _server("s1", env={"GITHUB_TOKEN": "from-config"})
        )
        env = client._build_env()
        assert env["GITHUB_TOKEN"] == "from-file"

    def test_comments_and_blanks_ignored(self, mcp_env_dir):
        _write_env_file(
            mcp_env_dir / "s1.env",
            "# comment\n\nA=1\n   \nB=2\n",
        )
        env = MCPStdioClient(_server("s1"))._build_env()
        assert env["A"] == "1"
        assert env["B"] == "2"

    def test_kiso_prefixed_keys_dropped(self, mcp_env_dir, caplog):
        _write_env_file(
            mcp_env_dir / "s1.env",
            "KISO_SECRET=nope\nGOOD=yes\n",
        )
        env = MCPStdioClient(_server("s1"))._build_env()
        assert "KISO_SECRET" not in env
        assert env["GOOD"] == "yes"

    def test_world_readable_file_skipped(self, mcp_env_dir):
        path = mcp_env_dir / "s1.env"
        _write_env_file(path, "GITHUB_TOKEN=abc\n", mode=0o644)
        env = MCPStdioClient(_server("s1"))._build_env()
        # mode 0644 is world-readable → refuse to load.
        assert "GITHUB_TOKEN" not in env

    def test_values_with_equals_in_value_preserved(self, mcp_env_dir):
        _write_env_file(mcp_env_dir / "s1.env", "URL=https://x?a=b&c=d\n")
        env = MCPStdioClient(_server("s1"))._build_env()
        assert env["URL"] == "https://x?a=b&c=d"

    def test_other_server_env_files_not_loaded(self, mcp_env_dir):
        _write_env_file(mcp_env_dir / "other.env", "OTHER_TOKEN=no\n")
        env = MCPStdioClient(_server("s1"))._build_env()
        assert "OTHER_TOKEN" not in env
