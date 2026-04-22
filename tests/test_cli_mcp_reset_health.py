"""Tests for ``kiso mcp reset-health <server>``.

Business requirement: when the circuit breaker trips on an MCP
server, the operator can reset it without restarting the daemon.
The command delegates to ``MCPManager.reset_health(name)``.
"""

from __future__ import annotations

import argparse

import pytest

from cli import mcp as cli_mcp


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class TestArgparseWiring:
    def test_reset_health_subcommand_parses(self):
        parser = argparse.ArgumentParser(prog="kiso")
        cli_mcp.add_subcommands(parser)
        args = parser.parse_args(["reset-health", "github"])
        assert args.mcp_command == "reset-health"
        assert args.name == "github"


class TestDispatch:
    def test_dispatch_calls_cmd_reset_health(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            cli_mcp, "_cmd_reset_health",
            lambda a: called.append(a.name) or 0,
        )
        rc = cli_mcp.handle(_ns(mcp_command="reset-health", name="github"))
        assert rc == 0
        assert called == ["github"]
