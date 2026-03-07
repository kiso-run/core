"""Shared fixtures for Docker integration tests.

These tests require root and run inside the dev container.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kiso.worker


@pytest.fixture()
def kiso_dir(tmp_path, monkeypatch):
    """Temporary KISO_DIR visible to sandbox users.

    Adds execute bits to parent dirs so sandbox users can traverse
    the path to reach the workspace.
    """
    monkeypatch.setattr(kiso.worker, "KISO_DIR", tmp_path)

    # Add o+x so sandbox users can descend into parent dirs.
    path = tmp_path
    while path != Path("/tmp") and path != path.parent:
        try:
            os.chmod(path, os.stat(path).st_mode | 0o011)
        except OSError:
            break
        path = path.parent

    return tmp_path
