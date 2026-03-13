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

    # --- .kiso config file write protection ---

    def test_env_direct_overwrite_blocked(self):
        assert check_command_deny_list("echo KISO_LLM_API_KEY=sk-x > ~/.kiso/.env") is not None

    def test_env_printf_overwrite_blocked(self):
        assert check_command_deny_list("printf 'KEY=%s\\n' val > ~/.kiso/.env") is not None

    def test_env_cat_heredoc_overwrite_blocked(self):
        assert check_command_deny_list("cat > ~/.kiso/.env << 'EOF'") is not None

    def test_env_append_blocked(self):
        assert check_command_deny_list("echo KEY=val >> ~/.kiso/.env") is not None

    def test_env_root_path_blocked(self):
        assert check_command_deny_list("echo KEY=val > /root/.kiso/.env") is not None

    def test_config_toml_overwrite_blocked(self):
        assert check_command_deny_list("cat > ~/.kiso/config.toml << 'EOF'") is not None

    def test_config_toml_root_path_blocked(self):
        assert check_command_deny_list("echo '[settings]' > /root/.kiso/config.toml") is not None

    def test_kiso_env_set_allowed(self):
        """kiso env set is the correct way — must NOT be blocked."""
        assert check_command_deny_list("kiso env set KISO_LLM_API_KEY sk-or-v1-abc") is None

    def test_env_write_session_file_allowed(self):
        """Writing to session workspace files is fine."""
        assert check_command_deny_list("echo hello > ~/.kiso/sessions/abc/output.txt") is None

    def test_other_env_file_allowed(self):
        """Writing to a project .env (not .kiso/) is allowed."""
        assert check_command_deny_list("echo KEY=val > /tmp/myproject/.env") is None

    # --- M84j: deny list bypass fixes ---

    def test_rm_rf_home_slash_blocked(self):
        """M84j: rm -rf ~/ (trailing slash) must be blocked."""
        assert check_command_deny_list("rm -rf ~/") is not None

    def test_rm_rf_home_env_slash_blocked(self):
        """M84j: rm -rf $HOME/ (trailing slash) must be blocked."""
        assert check_command_deny_list("rm -rf $HOME/") is not None

    def test_rm_r_home_blocked(self):
        """M84j: rm -r ~ (without -f) must also be blocked."""
        assert check_command_deny_list("rm -r ~") is not None

    def test_rm_r_root_blocked(self):
        """M84j: rm -r / must be blocked."""
        assert check_command_deny_list("rm -r /") is not None

    def test_fork_bomb_named_function_blocked(self):
        """M84j: named-function fork bomb variant must be blocked."""
        assert check_command_deny_list("a(){ a|a& }; a") is not None

    # --- M488: actionable hints ---

    def test_python_c_hint_contains_script_file(self):
        """M488: python -c denial includes script file alternative."""
        msg = check_command_deny_list('python3 -c "print(1)"')
        assert "Hint:" in msg
        assert "script.py" in msg

    def test_node_e_hint_contains_script_file(self):
        """M488: node -e denial includes script file alternative."""
        msg = check_command_deny_list('node -e "console.log(1)"')
        assert "Hint:" in msg
        assert "script.js" in msg

    def test_eval_hint_contains_alternative(self):
        """M488: eval denial includes direct command alternative."""
        msg = check_command_deny_list("eval echo hello")
        assert "Hint:" in msg
        assert "directly" in msg

    def test_rm_rf_root_hint(self):
        """M488: rm -rf / denial includes specific path hint."""
        msg = check_command_deny_list("rm -rf /")
        assert "Hint:" in msg
        assert "exact path" in msg

    def test_kiso_env_hint(self):
        """M488: .kiso/.env write denial suggests kiso env set."""
        msg = check_command_deny_list("echo KEY=val > ~/.kiso/.env")
        assert "Hint:" in msg
        assert "kiso env set" in msg

    def test_no_hint_for_generic_deny(self):
        """M488: patterns without hints don't include 'Hint:' in message."""
        msg = check_command_deny_list("dd if=/dev/zero of=/dev/sda")
        assert "Command blocked" in msg
        assert "Hint:" not in msg


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
        # Should have plaintext + base64 only (URL-encoded == plaintext, JSON == plaintext)
        assert "mysecret" in variants

    def test_json_escaped_variant(self):
        """M506: secrets with special chars get JSON-escaped variant."""
        variants = build_secret_variants('my\nsecret"key')
        # JSON escaping: \n → \\n, " → \"
        assert 'my\\nsecret\\"key' in variants

    def test_base64_error_logs_and_returns_plaintext(self):
        """If base64 encoding raises, log.error is called and plaintext is still returned."""
        import base64 as _b64
        from unittest.mock import patch
        with patch.object(_b64, "b64encode", side_effect=ValueError("encoding failed")):
            with patch("kiso.security.log") as mock_log:
                variants = build_secret_variants("mysecret")
        mock_log.error.assert_called_once()
        assert "mysecret" in variants  # plaintext always included


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

    def test_strips_json_escaped(self):
        """M506: JSON-escaped secret variant is stripped from output."""
        secret = 'key\nwith"quotes'
        result = sanitize_output(
            '{"token": "key\\nwith\\"quotes"}',
            {"KEY": secret},
            {},
        )
        assert 'key\\nwith\\"quotes' not in result
        assert "[REDACTED]" in result

    def test_empty_secrets_noop(self):
        """M506: no secrets → output returned unchanged (no regex compilation)."""
        result = sanitize_output("normal output", {}, {})
        assert result == "normal output"

    def test_cache_reused_on_same_secrets(self):
        """M547: compiled pattern is cached across calls with same secrets."""
        import kiso.security as sec
        secrets = {"KEY": "sk-abc123xyz"}
        sanitize_output("first call", secrets, {})
        cached_pattern = sec._sanitize_cache[1]
        assert cached_pattern is not None
        sanitize_output("second call", secrets, {})
        assert sec._sanitize_cache[1] is cached_pattern  # same object

    def test_cache_invalidated_on_new_secrets(self):
        """M547: cache is rebuilt when secret values change."""
        import kiso.security as sec
        sanitize_output("call 1", {"A": "secret-one-val"}, {})
        old_pattern = sec._sanitize_cache[1]
        sanitize_output("call 2", {"A": "secret-two-val"}, {})
        assert sec._sanitize_cache[1] is not old_pattern


class TestCollectDeploySecrets:
    def test_env_vars(self):
        env = {
            "KISO_TOOL_API_KEY": "sk-123",
            "KISO_CONNECTOR_TOKEN": "ct-456",
            "PATH": "/usr/bin",
            "HOME": "/home/user",
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets == {
            "KISO_TOOL_API_KEY": "sk-123",
            "KISO_CONNECTOR_TOKEN": "ct-456",
        }

    def test_env_vars_backward_compat_skill_prefix(self):
        """KISO_SKILL_* env vars still collected for backward compat."""
        env = {"KISO_SKILL_API_KEY": "sk-old"}
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets == {"KISO_SKILL_API_KEY": "sk-old"}

    def test_llm_api_key(self):
        env = {"KISO_LLM_API_KEY": "sk-test-key"}
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets["KISO_LLM_API_KEY"] == "sk-test-key"

    def test_missing_llm_api_key_skipped(self):
        with patch.dict(os.environ, {}, clear=True):
            secrets = collect_deploy_secrets()
        assert "KISO_LLM_API_KEY" not in secrets

    def test_accepts_no_arguments(self):
        """M66d: collect_deploy_secrets takes no parameters (config param removed)."""
        import inspect
        sig = inspect.signature(collect_deploy_secrets)
        assert len(sig.parameters) == 0, (
            "collect_deploy_secrets must have no parameters; "
            f"found: {list(sig.parameters)}"
        )

    def test_empty_env_returns_empty_dict(self):
        """No KISO_TOOL_*/KISO_CONNECTOR_* env vars → empty dict."""
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets == {}

    def test_non_kiso_prefixed_vars_excluded(self):
        """Non-kiso-prefixed vars must not appear in secrets."""
        env = {"MY_SECRET": "oops", "KISO_TOOL_X": "ok", "PATH": "/bin"}
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert "MY_SECRET" not in secrets
        assert "KISO_TOOL_X" in secrets


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
        "bob": User(role="user", tools=["search", "deploy"]),
        "charlie": User(role="user", tools="*"),
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
        result = revalidate_permissions(cfg, "bob", "skill", tool_name="search")
        assert result.allowed is True

    def test_revalidate_skill_denied(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "skill", tool_name="forbidden")
        assert result.allowed is False
        assert "not in user's allowed tools" in result.reason

    def test_revalidate_admin_all_skills(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "alice", "skill", tool_name="anything")
        assert result.allowed is True

    def test_revalidate_no_username(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, None, "exec")
        assert result.allowed is True
        assert result.role == "admin"

    def test_revalidate_wildcard_skills(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "charlie", "skill", tool_name="anything")
        assert result.allowed is True

    def test_revalidate_exec_allowed_for_user_role(self):
        """exec tasks allowed for user role (skill check only triggers for skill tasks)."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "exec")
        assert result.allowed is True
        assert result.role == "user"

    def test_revalidate_skill_name_none_skips_check(self):
        """tool_name=None with task_type='skill' skips tool-level check."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "skill", tool_name=None)
        assert result.allowed is True

    def test_revalidate_returns_tools_field(self):
        """PermissionResult.tools populated from user config."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "exec")
        assert result.tools == ["search", "deploy"]

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
        assert result.tools == ["search", "deploy"]


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
