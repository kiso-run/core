"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from kiso.config import load_config
from kiso.main import app

VALID_CONFIG = """\
[tokens]
cli = "test-secret-token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "KISO_OPENROUTER_API_KEY"

[users.testadmin]
role = "admin"

[users.testuser]
role = "user"
skills = "*"
"""


@pytest.fixture()
def test_config_path(tmp_path: Path) -> Path:
    """Write a valid config.toml to tmp_path and return its Path."""
    p = tmp_path / "config.toml"
    p.write_text(VALID_CONFIG)
    return p


@pytest.fixture()
def test_config(test_config_path: Path):
    """Load and return a Config from the test config file."""
    return load_config(test_config_path)


@pytest_asyncio.fixture()
async def client(test_config_path: Path):
    """Async httpx client wired to the FastAPI app via ASGI transport.

    Patches load_config so the real lifespan loads the test config.
    """
    with patch("kiso.main.load_config", lambda: load_config(test_config_path)):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
