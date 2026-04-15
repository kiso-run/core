"""Tests for the MCP credentials store (M1372).

The store is a tiny file-based persistence layer for OAuth tokens
(and any other per-server secret kiso may need to remember across
sessions). Each server gets its own file under
``~/.kiso/mcp/credentials/<server>.json`` with mode ``0600``,
owned by the runtime user.

Why a custom store and not env vars: OAuth tokens are obtained
*at runtime* via interactive flows (device flow, browser redirect),
so they cannot be pre-set in the environment. They also rotate
on refresh and need to survive between kiso processes within the
same session/install.

Refresh-token handling is intentionally out of scope for v0.9 —
expired tokens just trigger a re-run of the original flow. The
store carries whatever JSON the auth provider returned plus a
`saved_at` timestamp, and the caller decides whether the value
is still usable.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from kiso.mcp.credentials import (
    CredentialsError,
    delete_credential,
    load_credential,
    save_credential,
    server_credentials_path,
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_path_under_kiso_dir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        path = server_credentials_path("github")
        assert path == tmp_path / "mcp" / "credentials" / "github.json"

    def test_path_rejects_path_traversal(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        with pytest.raises(CredentialsError):
            server_credentials_path("../etc/passwd")

    def test_path_rejects_empty_name(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        with pytest.raises(CredentialsError):
            server_credentials_path("")

    def test_path_rejects_slash_in_name(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        with pytest.raises(CredentialsError):
            server_credentials_path("github/foo")


# ---------------------------------------------------------------------------
# save / load / delete
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_save_then_load_round_trip(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        token = {
            "access_token": "ghp_abc",
            "token_type": "bearer",
            "scope": "repo,user",
        }
        save_credential("github", token)
        loaded = load_credential("github")
        assert loaded is not None
        # round-trip preserves the auth provider data
        assert loaded["access_token"] == "ghp_abc"
        assert loaded["token_type"] == "bearer"
        assert loaded["scope"] == "repo,user"
        # plus a saved_at timestamp injected by the store
        assert "saved_at" in loaded
        assert isinstance(loaded["saved_at"], (int, float))
        assert loaded["saved_at"] <= time.time() + 1

    def test_load_missing_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        assert load_credential("never_saved") is None

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch) -> None:
        # tmp_path/mcp/credentials does not exist yet
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        save_credential("github", {"access_token": "x"})
        assert (tmp_path / "mcp" / "credentials" / "github.json").is_file()

    def test_save_overwrites_previous(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        save_credential("github", {"access_token": "old"})
        save_credential("github", {"access_token": "new"})
        loaded = load_credential("github")
        assert loaded["access_token"] == "new"

    def test_delete_removes_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        save_credential("github", {"access_token": "x"})
        delete_credential("github")
        assert load_credential("github") is None

    def test_delete_missing_is_noop(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        # Must not raise
        delete_credential("never_existed")


# ---------------------------------------------------------------------------
# Permissions and on-disk format
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_file_mode_is_0600(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        save_credential("github", {"access_token": "x"})
        path = tmp_path / "mcp" / "credentials" / "github.json"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, (
            f"credential file must be mode 0600 (no group/other access), "
            f"got {oct(mode)}"
        )

    def test_directory_mode_is_0700(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        save_credential("github", {"access_token": "x"})
        creds_dir = tmp_path / "mcp" / "credentials"
        mode = stat.S_IMODE(creds_dir.stat().st_mode)
        assert mode == 0o700, (
            f"credentials directory must be mode 0700 (no group/other "
            f"access), got {oct(mode)}"
        )

    def test_file_contains_valid_json(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        save_credential("github", {"access_token": "secret"})
        path = tmp_path / "mcp" / "credentials" / "github.json"
        # Decoded as JSON (not pickled or otherwise opaque)
        content = json.loads(path.read_text())
        assert content["access_token"] == "secret"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_save_invalid_json_payload_raises(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        # Sets aren't JSON-serializable
        with pytest.raises(CredentialsError):
            save_credential("github", {"bad": {1, 2, 3}})

    def test_load_corrupted_file_raises(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiso.mcp.credentials.KISO_DIR", tmp_path
        )
        path = tmp_path / "mcp" / "credentials" / "github.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        path.chmod(0o600)
        with pytest.raises(CredentialsError):
            load_credential("github")
