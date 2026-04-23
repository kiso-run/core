"""CLI tests for kiso behavior commands."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from tests._cli_test_helpers import mock_cli_config, make_cli_args, mock_http_response


class TestBehaviorList:
    def test_list_empty(self, capsys):
        from cli.behavior import behavior_list
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": []}):
            behavior_list(args)
        assert "No behavioral guidelines" in capsys.readouterr().out

    def test_list_with_behaviors(self, capsys):
        from cli.behavior import behavior_list
        args = make_cli_args()
        facts = [
            {"id": 1, "content": "Always respond formally"},
            {"id": 2, "content": "Use metrics in every answer"},
        ]
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": facts}):
            behavior_list(args)
        out = capsys.readouterr().out
        assert "[1]" in out
        assert "formally" in out
        assert "metrics" in out


class TestBehaviorAdd:
    def test_add_basic(self, capsys):
        from cli.behavior import behavior_add
        args = make_cli_args(content="Always respond formally")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli._admin.require_admin"), \
             mock_http_response({"id": 10, "content": "Always respond formally", "category": "behavior"}):
            behavior_add(args)
        out = capsys.readouterr().out
        assert "id=10" in out

    def test_add_empty_rejected(self, capsys):
        from cli.behavior import behavior_add
        args = make_cli_args(content="  ")
        with patch("cli._admin.require_admin"), \
             pytest.raises(SystemExit):
            behavior_add(args)
        assert "empty" in capsys.readouterr().err


class TestBehaviorRemove:
    def test_remove_success(self, capsys):
        from cli.behavior import behavior_remove
        args = make_cli_args(behavior_id=10)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli._admin.require_admin"), \
             mock_http_response({"deleted": True}):
            behavior_remove(args)
        assert "10 removed" in capsys.readouterr().out
