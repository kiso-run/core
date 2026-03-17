"""M674: CLI tests for kiso behavior commands."""

from __future__ import annotations

import argparse
from unittest.mock import patch, MagicMock

import pytest


def _mock_config():
    cfg = MagicMock()
    cfg.tokens = {"cli": "tok-abc"}
    return cfg


def _make_args(**kwargs):
    defaults = {"api": "http://localhost:8333"}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _mock_http(return_value):
    mock_resp = MagicMock()
    mock_resp.json.return_value = return_value
    mock_resp.raise_for_status = MagicMock()
    return patch("httpx.request", return_value=mock_resp)


class TestBehaviorList:
    def test_list_empty(self, capsys):
        from cli.behavior import behavior_list
        args = _make_args()
        with patch("kiso.config.load_config", return_value=_mock_config()), \
             _mock_http({"facts": []}):
            behavior_list(args)
        assert "No behavioral guidelines" in capsys.readouterr().out

    def test_list_with_behaviors(self, capsys):
        from cli.behavior import behavior_list
        args = _make_args()
        facts = [
            {"id": 1, "content": "Always respond formally"},
            {"id": 2, "content": "Use metrics in every answer"},
        ]
        with patch("kiso.config.load_config", return_value=_mock_config()), \
             _mock_http({"facts": facts}):
            behavior_list(args)
        out = capsys.readouterr().out
        assert "[1]" in out
        assert "formally" in out
        assert "metrics" in out


class TestBehaviorAdd:
    def test_add_basic(self, capsys):
        from cli.behavior import behavior_add
        args = _make_args(content="Always respond formally")
        with patch("kiso.config.load_config", return_value=_mock_config()), \
             patch("cli.plugin_ops.require_admin"), \
             _mock_http({"id": 10, "content": "Always respond formally", "category": "behavior"}):
            behavior_add(args)
        out = capsys.readouterr().out
        assert "id=10" in out

    def test_add_empty_rejected(self, capsys):
        from cli.behavior import behavior_add
        args = _make_args(content="  ")
        with patch("cli.plugin_ops.require_admin"), \
             pytest.raises(SystemExit):
            behavior_add(args)
        assert "empty" in capsys.readouterr().err


class TestBehaviorRemove:
    def test_remove_success(self, capsys):
        from cli.behavior import behavior_remove
        args = _make_args(behavior_id=10)
        with patch("kiso.config.load_config", return_value=_mock_config()), \
             patch("cli.plugin_ops.require_admin"), \
             _mock_http({"deleted": True}):
            behavior_remove(args)
        assert "10 removed" in capsys.readouterr().out
