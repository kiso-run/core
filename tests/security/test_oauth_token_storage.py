"""Concern 4 — MCP OAuth credentials are stored safely on disk.

Invariants:
- ``~/.kiso/mcp/credentials/`` is mode ``0700``.
- Each ``<name>.json`` file inside is mode ``0600``.
- Reads refuse to follow symlinks — a planted symlink at the
  credential path must not cause kiso to read or write through
  it. Symlink-follow would let another user on the host exfiltrate
  (read) or overwrite (write) a credential by pre-creating a link
  to their own file.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from kiso.mcp.credentials import (
    CredentialsError,
    load_credential,
    save_credential,
    server_credentials_path,
)


@pytest.fixture
def kiso_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiso.config.KISO_DIR", tmp_path)
    monkeypatch.setattr("kiso.mcp.credentials.KISO_DIR", tmp_path)
    return tmp_path


class TestCredentialFileModes:
    def test_dir_mode_is_0700(self, kiso_dir):
        save_credential("demo", {"access_token": "tok"})
        creds_dir = kiso_dir / "mcp" / "credentials"
        assert creds_dir.is_dir()
        mode = stat.S_IMODE(creds_dir.stat().st_mode)
        assert mode == 0o700, oct(mode)

    def test_file_mode_is_0600(self, kiso_dir):
        path = save_credential("demo", {"access_token": "tok"})
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, oct(mode)


class TestSymlinkRefusal:
    def test_save_refuses_to_follow_preexisting_symlink(self, kiso_dir, tmp_path):
        # An attacker plants a symlink at the credential path pointing
        # at a file they control. If save_credential() follows the
        # symlink, the attacker-owned file gets overwritten under the
        # runtime user's privileges.
        path = server_credentials_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        attacker_target = tmp_path / "attacker_target"
        attacker_target.write_text("sentinel\n")
        path.symlink_to(attacker_target)

        with pytest.raises(CredentialsError):
            save_credential("demo", {"access_token": "tok"})

        # The attacker-owned file must not have been overwritten.
        assert attacker_target.read_text() == "sentinel\n"

    def test_load_refuses_to_follow_preexisting_symlink(self, kiso_dir, tmp_path):
        # Symmetric check for reads — a symlink must not be followed
        # out of the credentials directory to an arbitrary path.
        path = server_credentials_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        attacker_target = tmp_path / "attacker_target.json"
        attacker_target.write_text(json.dumps({"access_token": "leaked"}))
        path.symlink_to(attacker_target)

        with pytest.raises(CredentialsError):
            load_credential("demo")
