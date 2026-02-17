"""Tests for kiso/security.py — exec deny list, secret sanitization, fencing."""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

import pytest

from kiso.config import Config, Provider
from kiso.security import (
    build_secret_variants,
    check_command_deny_list,
    collect_deploy_secrets,
    escape_fence_delimiters,
    fence_content,
    sanitize_output,
)


# --- Exec deny list ---


class TestCheckCommandDenyList:
    def test_rm_rf_root_blocked(self):
        assert check_command_deny_list("rm -rf /") is not None

    def test_rm_rf_home_blocked(self):
        assert check_command_deny_list("rm -rf ~") is not None

    def test_rm_rf_env_home_blocked(self):
        assert check_command_deny_list("rm -rf $HOME") is not None

    def test_rm_fr_root_blocked(self):
        """Flags reversed: rm -fr /"""
        assert check_command_deny_list("rm -fr /") is not None

    def test_rm_rf_relative_allowed(self):
        assert check_command_deny_list("rm -rf ./build/") is None

    def test_rm_rf_named_dir_allowed(self):
        assert check_command_deny_list("rm -rf /tmp/mydir") is None

    def test_dd_if_blocked(self):
        assert check_command_deny_list("dd if=/dev/zero of=/dev/sda") is not None

    def test_mkfs_blocked(self):
        assert check_command_deny_list("mkfs.ext4 /dev/sda1") is not None

    def test_chmod_777_root_blocked(self):
        assert check_command_deny_list("chmod -R 777 /") is not None

    def test_chmod_644_allowed(self):
        assert check_command_deny_list("chmod 644 file.txt") is None

    def test_chown_recursive_blocked(self):
        assert check_command_deny_list("chown -R root:root /var") is not None

    def test_shutdown_blocked(self):
        assert check_command_deny_list("shutdown -h now") is not None

    def test_reboot_blocked(self):
        assert check_command_deny_list("reboot") is not None

    def test_fork_bomb_blocked(self):
        assert check_command_deny_list(":(){ :|:& };:") is not None

    def test_normal_commands_allowed(self):
        for cmd in ["ls -la", "echo hello", "git status", "python3 script.py"]:
            assert check_command_deny_list(cmd) is None, f"Should allow: {cmd}"


# --- Secret sanitization ---


class TestBuildSecretVariants:
    def test_plaintext(self):
        variants = build_secret_variants("mysecret")
        assert "mysecret" in variants

    def test_base64(self):
        variants = build_secret_variants("mysecret")
        b64 = base64.b64encode(b"mysecret").decode()
        assert b64 in variants

    def test_url_encoded(self):
        variants = build_secret_variants("my secret&key")
        assert "my%20secret%26key" in variants

    def test_short_skipped(self):
        assert build_secret_variants("ab") == []
        assert build_secret_variants("abc") == []

    def test_empty_skipped(self):
        assert build_secret_variants("") == []


class TestSanitizeOutput:
    def test_strips_plaintext(self):
        result = sanitize_output(
            "token is sk-abc123xyz",
            {"KEY": "sk-abc123xyz"},
            {},
        )
        assert "sk-abc123xyz" not in result
        assert "[REDACTED]" in result

    def test_strips_base64(self):
        secret = "sk-abc123xyz"
        b64 = base64.b64encode(secret.encode()).decode()
        result = sanitize_output(
            f"encoded: {b64}",
            {"KEY": secret},
            {},
        )
        assert b64 not in result
        assert "[REDACTED]" in result

    def test_strips_ephemeral(self):
        result = sanitize_output(
            "ephemeral: eph-secret-val",
            {},
            {"TEMP": "eph-secret-val"},
        )
        assert "eph-secret-val" not in result
        assert "[REDACTED]" in result

    def test_no_false_positives(self):
        result = sanitize_output(
            "normal output with no secrets",
            {"KEY": "sk-abc123xyz"},
            {},
        )
        assert result == "normal output with no secrets"

    def test_longest_first(self):
        """Overlapping values: longer match replaced first."""
        result = sanitize_output(
            "value: supersecretkey",
            {"A": "supersecretkey", "B": "secret"},
            {},
        )
        # "supersecretkey" should be replaced as one unit, not partially
        assert result == "value: [REDACTED]"


class TestCollectDeploySecrets:
    def test_env_vars(self):
        env = {
            "KISO_SKILL_API_KEY": "sk-123",
            "KISO_CONNECTOR_TOKEN": "ct-456",
            "PATH": "/usr/bin",
            "HOME": "/home/user",
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets == {
            "KISO_SKILL_API_KEY": "sk-123",
            "KISO_CONNECTOR_TOKEN": "ct-456",
        }

    def test_provider_keys(self):
        config = Config(
            tokens={},
            providers={"openai": Provider(base_url="https://api.openai.com", api_key_env="OPENAI_KEY")},
            users={},
            models={},
            settings={},
            raw={},
        )
        env = {"OPENAI_KEY": "sk-openai-test"}
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets(config)
        assert secrets["OPENAI_KEY"] == "sk-openai-test"

    def test_missing_provider_env_skipped(self):
        config = Config(
            tokens={},
            providers={"openai": Provider(base_url="https://api.openai.com", api_key_env="MISSING_KEY")},
            users={},
            models={},
            settings={},
            raw={},
        )
        with patch.dict(os.environ, {}, clear=True):
            secrets = collect_deploy_secrets(config)
        assert "MISSING_KEY" not in secrets


# --- Random boundary fencing ---


class TestFenceContent:
    def test_fence_content_has_random_token(self):
        result = fence_content("hello", "TEST")
        assert result.startswith("<<<TEST_")
        assert ">>>" in result
        assert "hello" in result
        assert "<<<END_TEST_" in result

    def test_fence_content_unique_tokens(self):
        r1 = fence_content("a", "LABEL")
        r2 = fence_content("a", "LABEL")
        # Extract the full markers — they should differ
        assert r1 != r2

    def test_escape_fence_delimiters(self):
        assert escape_fence_delimiters("<<<hello>>>") == "«««hello»»»"
        assert escape_fence_delimiters("normal text") == "normal text"
        assert escape_fence_delimiters("<<<a>>> and <<<b>>>") == "«««a»»» and «««b»»»"

    def test_fence_escapes_before_wrapping(self):
        """Pre-crafted delimiters in content are escaped before fencing."""
        result = fence_content("<<<FAKE_TOKEN>>>", "REAL")
        # The content should have escaped delimiters
        assert "«««FAKE_TOKEN»»»" in result
        # But the outer fence should use real delimiters
        assert result.startswith("<<<REAL_")
        assert result.endswith(">>>")
