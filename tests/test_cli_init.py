"""Tests for ``kiso init`` — preset bootstrap UX."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.init import run_init_command


def _make_args(**kwargs) -> argparse.Namespace:
    defaults = {"preset": "default", "force": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture
def isolated_kiso_dir(tmp_path, monkeypatch):
    """Redirect ~/.kiso to a tmp dir so tests can't touch real config."""
    kiso_dir = tmp_path / ".kiso"
    config_path = kiso_dir / "config.toml"
    monkeypatch.setattr("cli.init.CONFIG_PATH", config_path)
    return config_path


class TestInitCommand:
    def test_creates_config_with_default_preset(self, isolated_kiso_dir, capsys):
        rc = run_init_command(_make_args())
        assert rc == 0
        assert isolated_kiso_dir.is_file()
        content = isolated_kiso_dir.read_text(encoding="utf-8")
        # Base template + preset-rendered MCP sections
        assert "[tokens]" in content
        assert "[mcp.filesystem]" in content
        assert "[mcp.aider]" in content
        # Post-init message
        out = capsys.readouterr().out
        assert "Config created" in out
        assert "OPENROUTER_API_KEY" in out
        assert "GITHUB_TOKEN" in out

    def test_none_preset_writes_template_only(self, isolated_kiso_dir):
        rc = run_init_command(_make_args(preset="none"))
        assert rc == 0
        content = isolated_kiso_dir.read_text(encoding="utf-8")
        assert "[tokens]" in content
        assert "[mcp." not in content

    def test_refuses_overwrite_without_force(self, isolated_kiso_dir, capsys):
        isolated_kiso_dir.parent.mkdir(parents=True, exist_ok=True)
        isolated_kiso_dir.write_text("pre-existing\n", encoding="utf-8")
        rc = run_init_command(_make_args())
        assert rc == 1
        assert isolated_kiso_dir.read_text() == "pre-existing\n"
        err = capsys.readouterr().err
        assert "already exists" in err
        assert "--force" in err

    def test_force_overwrites_existing(self, isolated_kiso_dir):
        isolated_kiso_dir.parent.mkdir(parents=True, exist_ok=True)
        isolated_kiso_dir.write_text("pre-existing\n", encoding="utf-8")
        rc = run_init_command(_make_args(force=True))
        assert rc == 0
        content = isolated_kiso_dir.read_text()
        assert content != "pre-existing\n"
        assert "[mcp.filesystem]" in content

    def test_unknown_preset_exits_non_zero(self, isolated_kiso_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            run_init_command(_make_args(preset="does-not-exist"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err


class TestInitConfigParsesWithKisoRuntime:
    """Written config must survive kiso's own parser + env substitution."""

    def test_rendered_config_is_loadable(self, isolated_kiso_dir, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")
        monkeypatch.setenv("HOME", "/tmp/test-home")
        run_init_command(_make_args())

        import tomllib
        from kiso.mcp.config import parse_mcp_section

        parsed = tomllib.loads(isolated_kiso_dir.read_text())
        servers = parse_mcp_section(parsed.get("mcp"))
        assert len(servers) == 9
        assert servers["filesystem"].command == "npx"
        assert servers["aider"].env["OPENROUTER_API_KEY"] == "sk-test"


class TestInitParserRegistration:
    """The top-level parser must expose `init` with expected flags."""

    def test_init_subcommand_exists(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["init"])
        assert args.command == "init"
        assert args.preset == "default"
        assert args.force is False

    def test_init_accepts_preset_flag(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["init", "--preset", "none"])
        assert args.preset == "none"

    def test_init_accepts_force_flag(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["init", "--force"])
        assert args.force is True
