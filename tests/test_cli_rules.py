"""CLI tests for safety rules management commands (cli/rules.py)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests._cli_test_helpers import make_cli_args, mock_cli_config, mock_http_response


# ── rules_list ────────────────────────────────────────────────


class TestRulesList:
    def test_empty(self, capsys):
        from cli.rules import rules_list

        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"rules": []}):
            rules_list(args)
        assert "No safety rules configured" in capsys.readouterr().out

    def test_multiple_rules(self, capsys):
        from cli.rules import rules_list

        rules = [
            {"id": 1, "content": "Never delete system files"},
            {"id": 2, "content": "Always confirm destructive actions"},
        ]
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"rules": rules}):
            rules_list(args)
        out = capsys.readouterr().out
        assert "[1]" in out
        assert "Never delete system files" in out
        assert "[2]" in out
        assert "destructive" in out


# ── rules_add ─────────────────────────────────────────────────


class TestRulesAdd:
    def test_happy_path(self, capsys):
        from cli.rules import rules_add

        args = make_cli_args(rule_content="No network access after 6pm")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"id": 10, "content": "No network access after 6pm"}):
            rules_add(args)
        out = capsys.readouterr().out
        assert "id=10" in out
        assert "No network access" in out

    def test_empty_content(self, capsys):
        from cli.rules import rules_add

        args = make_cli_args(rule_content="  ")
        with patch("cli.plugin_ops.require_admin"), \
             pytest.raises(SystemExit):
            rules_add(args)
        assert "empty" in capsys.readouterr().err

    def test_require_admin(self):
        from cli.rules import rules_add

        args = make_cli_args(rule_content="Some rule")
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            rules_add(args)


# ── rules_remove ──────────────────────────────────────────────


class TestRulesRemove:
    def test_deleted(self, capsys):
        from cli.rules import rules_remove

        args = make_cli_args(rule_id=5)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"deleted": True}):
            rules_remove(args)
        assert "5 removed" in capsys.readouterr().out

    def test_not_deleted(self, capsys):
        from cli.rules import rules_remove

        args = make_cli_args(rule_id=99)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"deleted": False}), \
             pytest.raises(SystemExit):
            rules_remove(args)
        assert "could not remove" in capsys.readouterr().err

    def test_require_admin(self):
        from cli.rules import rules_remove

        args = make_cli_args(rule_id=1)
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            rules_remove(args)
