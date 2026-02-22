"""Tests for kiso/security.py — exec deny list, secret sanitization, fencing."""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

import pytest

from kiso.config import Config, Provider, User
from kiso.security import (
    PermissionResult,
    build_secret_variants,
    check_command_deny_list,
    collect_deploy_secrets,
    escape_fence_delimiters,
    fence_content,
    revalidate_permissions,
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

    def test_blocks_pipe_to_dangerous(self):
        assert check_command_deny_list("echo hello | rm -rf /") is not None

    def test_blocks_semicolon_dangerous(self):
        assert check_command_deny_list("echo hello; rm -rf /") is not None

    def test_blocks_and_dangerous(self):
        assert check_command_deny_list("echo hello && rm -rf /") is not None

    def test_blocks_or_dangerous(self):
        assert check_command_deny_list("echo hello || rm -rf /") is not None

    def test_blocks_newline_dangerous(self):
        assert check_command_deny_list("echo hello\nrm -rf /") is not None

    def test_blocks_command_substitution(self):
        assert check_command_deny_list("echo $(rm -rf /)") is not None

    def test_blocks_backtick_substitution(self):
        assert check_command_deny_list("echo `rm -rf /`") is not None

    def test_allows_safe_pipe(self):
        assert check_command_deny_list("ls | grep foo") is None

    def test_allows_safe_semicolon(self):
        assert check_command_deny_list("echo a; echo b") is None

    # --- 21a: deny list bypass patterns ---

    def test_base64_pipe_to_sh_blocked(self):
        assert check_command_deny_list("echo cm0gLXJmIC8= | base64 -d | sh") is not None

    def test_base64_pipe_to_bash_blocked(self):
        assert check_command_deny_list("echo foo | base64 -d | bash") is not None

    def test_base64_pipe_to_zsh_blocked(self):
        assert check_command_deny_list("echo foo | base64 -d | zsh") is not None

    def test_python_c_blocked(self):
        assert check_command_deny_list('python3 -c "import os; os.system(\'rm -rf /\')"') is not None

    def test_python2_c_blocked(self):
        assert check_command_deny_list('python2 -c "print(1)"') is not None

    def test_perl_e_blocked(self):
        assert check_command_deny_list('perl -e "system(\'rm -rf /\')"') is not None

    def test_ruby_e_blocked(self):
        assert check_command_deny_list('ruby -e "system(\'rm -rf /\')"') is not None

    def test_eval_blocked(self):
        assert check_command_deny_list("eval $(printf '\\x72\\x6d -rf /')") is not None

    def test_node_e_blocked(self):
        assert check_command_deny_list('node -e "require(\'child_process\').exec(\'rm -rf /\')"') is not None

    # --- 21a: safe uses NOT blocked ---

    def test_python3_script_allowed(self):
        assert check_command_deny_list("python3 script.py") is None

    def test_node_app_allowed(self):
        assert check_command_deny_list("node app.js") is None

    def test_echo_base64_allowed(self):
        assert check_command_deny_list("echo hello | base64") is None

    def test_perl_script_allowed(self):
        assert check_command_deny_list("perl script.pl") is None

    def test_ruby_script_allowed(self):
        assert check_command_deny_list("ruby script.rb") is None

    def test_node_no_flag_allowed(self):
        assert check_command_deny_list("node server.js --port 3000") is None

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

    def test_exactly_four_chars_included(self):
        """4-char boundary: len < 4 skipped, len == 4 included."""
        variants = build_secret_variants("abcd")
        assert len(variants) >= 1
        assert "abcd" in variants

    def test_plain_ascii_no_url_variant(self):
        """Plain ASCII: URL-encoding produces same string, no extra variant."""
        variants = build_secret_variants("mysecret")
        # Should have plaintext + base64 only (URL-encoded == plaintext)
        assert "mysecret" in variants
        # URL-encoded form is identical, so not duplicated
        assert len(variants) == 2


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

    def test_llm_api_key(self):
        env = {"KISO_LLM_API_KEY": "sk-test-key"}
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets["KISO_LLM_API_KEY"] == "sk-test-key"

    def test_missing_llm_api_key_skipped(self):
        with patch.dict(os.environ, {}, clear=True):
            secrets = collect_deploy_secrets()
        assert "KISO_LLM_API_KEY" not in secrets


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

    def test_fence_empty_content(self):
        """Empty string content is fenced correctly."""
        result = fence_content("", "LABEL")
        assert result.startswith("<<<LABEL_")
        assert "<<<END_LABEL_" in result
        # Content area between markers is just the newlines
        lines = result.split("\n")
        assert lines[1] == ""  # empty content line

    def test_fence_token_length(self):
        """Token is 32 hex chars (16 bytes)."""
        result = fence_content("x", "T")
        # Extract token: <<<T_{token}>>>
        marker = result.split(">>>")[0].replace("<<<T_", "")
        assert len(marker) == 32
        assert all(c in "0123456789abcdef" for c in marker)

    def test_fence_escapes_before_wrapping(self):
        """Pre-crafted delimiters in content are escaped before fencing."""
        result = fence_content("<<<FAKE_TOKEN>>>", "REAL")
        # The content should have escaped delimiters
        assert "«««FAKE_TOKEN»»»" in result
        # But the outer fence should use real delimiters
        assert result.startswith("<<<REAL_")
        assert result.endswith(">>>")


# --- Permission re-validation ---


def _perm_config(**user_overrides) -> Config:
    users = {
        "alice": User(role="admin"),
        "bob": User(role="user", skills=["search", "deploy"]),
        "charlie": User(role="user", skills="*"),
    }
    users.update(user_overrides)
    return Config(
        tokens={}, providers={}, users=users,
        models={}, settings={}, raw={},
    )


class TestRevalidatePermissions:
    def test_revalidate_user_exists(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "alice", "exec")
        assert result.allowed is True
        assert result.role == "admin"

    def test_revalidate_user_removed(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "unknown", "exec")
        assert result.allowed is False
        assert "no longer exists" in result.reason

    def test_revalidate_skill_allowed(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "skill", skill_name="search")
        assert result.allowed is True

    def test_revalidate_skill_denied(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "skill", skill_name="forbidden")
        assert result.allowed is False
        assert "not in user's allowed skills" in result.reason

    def test_revalidate_admin_all_skills(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "alice", "skill", skill_name="anything")
        assert result.allowed is True

    def test_revalidate_no_username(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, None, "exec")
        assert result.allowed is True
        assert result.role == "admin"

    def test_revalidate_wildcard_skills(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "charlie", "skill", skill_name="anything")
        assert result.allowed is True

    def test_revalidate_exec_allowed_for_user_role(self):
        """exec tasks allowed for user role (skill check only triggers for skill tasks)."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "exec")
        assert result.allowed is True
        assert result.role == "user"

    def test_revalidate_skill_name_none_skips_check(self):
        """skill_name=None with task_type='skill' skips skill-level check."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "skill", skill_name=None)
        assert result.allowed is True

    def test_revalidate_returns_skills_field(self):
        """PermissionResult.skills populated from user config."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "exec")
        assert result.skills == ["search", "deploy"]

    def test_revalidate_msg_allowed_for_user_role(self):
        """msg tasks allowed for user role."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "msg")
        assert result.allowed is True

    def test_revalidate_search_always_allowed(self):
        """search task type is always allowed, no matter the user role."""
        cfg = _perm_config()
        # Admin
        result = revalidate_permissions(cfg, "alice", "search")
        assert result.allowed is True
        assert result.role == "admin"
        # Regular user
        result = revalidate_permissions(cfg, "bob", "search")
        assert result.allowed is True
        assert result.role == "user"
        assert result.skills == ["search", "deploy"]


# --- Double masking proof ---


class TestNoDoubleMasking:
    def test_no_double_masking_redacted_secret(self):
        """A secret value of '[REDACTED]' should produce a single [REDACTED], not nested."""
        result = sanitize_output(
            "value is [REDACTED]",
            {"KEY": "[REDACTED]"},
            {},
        )
        assert result == "value is [REDACTED]"
        # Should NOT produce something like [[REDACTED]]
        assert "[[REDACTED]]" not in result
