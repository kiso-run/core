"""Shared test helpers for CLI command tests.

Extracted from test_cli_knowledge.py, test_cli_behavior.py, test_presets.py,
test_cli.py, test_cli_session.py, test_cli_env.py.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch


def mock_cli_config(has_cli_token: bool = True):
    """Create a MagicMock config with CLI token for CLI tests."""
    cfg = MagicMock()
    cfg.tokens = {"cli": "tok-abc"} if has_cli_token else {}
    return cfg


def make_cli_args(api: str = "http://localhost:8333", **kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace for CLI function tests."""
    defaults = {"api": api}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def mock_http_response(return_value: dict):
    """Create a context manager that patches httpx.request with a mock response."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = return_value
    mock_resp.raise_for_status = MagicMock()
    return patch("httpx.request", return_value=mock_resp)
