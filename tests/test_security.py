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
    sanitize_value,
)


# --- Exec deny list ---


# (cmd, should_block, hint_contains) — hint_contains is None or a substring to check
_DENY_LIST_CASES = [
    # --- Core destructive commands ---
    ("rm -rf /", True, "exact path"),
    ("rm -rf ~", True, None),
    ("rm -rf $HOME", True, None),
    ("rm -fr /", True, None),  # flags reversed
    ("rm -rf ./build/", False, None),
    ("rm -rf /tmp/mydir", False, None),
    ("dd if=/dev/zero of=/dev/sda", True, None),
    ("mkfs.ext4 /dev/sda1", True, None),
    ("chmod -R 777 /", True, None),
    ("chmod 644 file.txt", False, None),
    ("chown -R root:root /var", True, None),
    ("shutdown -h now", True, None),
    ("reboot", True, None),
    (":(){ :|:& };:", True, None),
    # --- Compound commands with dangerous segments ---
    ("echo hello | rm -rf /", True, None),
    ("echo hello; rm -rf /", True, None),
    ("echo hello && rm -rf /", True, None),
    ("echo hello || rm -rf /", True, None),
    ("echo hello\nrm -rf /", True, None),
    ("echo $(rm -rf /)", True, None),
    ("echo `rm -rf /`", True, None),
    ("ls | grep foo", False, None),
    ("echo a; echo b", False, None),
    # --- 21a: deny list bypass patterns ---
    ("echo cm0gLXJmIC8= | base64 -d | sh", True, None),
    ("echo foo | base64 -d | bash", True, None),
    ("echo foo | base64 -d | zsh", True, None),
    ('python3 -c "import os; os.system(\'rm -rf /\')"', True, None),
    ('python2 -c "print(1)"', True, None),
    ('perl -e "system(\'rm -rf /\')"', True, None),
    ('ruby -e "system(\'rm -rf /\')"', True, None),
    ("eval $(printf '\\x72\\x6d -rf /')", True, None),
    ('node -e "require(\'child_process\').exec(\'rm -rf /\')"', True, None),
    # --- 21a: safe uses NOT blocked ---
    ("python3 script.py", False, None),
    ("node app.js", False, None),
    ("echo hello | base64", False, None),
    ("perl script.pl", False, None),
    ("ruby script.rb", False, None),
    ("node server.js --port 3000", False, None),
    ("ls -la", False, None),
    ("echo hello", False, None),
    ("git status", False, None),
    # --- .kiso config file write protection ---
    ("echo KISO_LLM_API_KEY=sk-x > ~/.kiso/.env", True, "kiso env set"),
    ("printf 'KEY=%s\\n' val > ~/.kiso/.env", True, None),
    ("cat > ~/.kiso/.env << 'EOF'", True, None),
    ("echo KEY=val >> ~/.kiso/.env", True, None),
    ("echo KEY=val > /root/.kiso/.env", True, None),
    ("cat > ~/.kiso/config.toml << 'EOF'", True, None),
    ("echo '[settings]' > /root/.kiso/config.toml", True, None),
    ("kiso env set KISO_LLM_API_KEY sk-or-v1-abc", False, None),
    ("echo hello > ~/.kiso/sessions/abc/output.txt", False, None),
    ("echo KEY=val > /tmp/myproject/.env", False, None),
    # --- M84j: deny list bypass fixes ---
    ("rm -rf ~/", True, None),
    ("rm -rf $HOME/", True, None),
    ("rm -r ~", True, None),
    ("rm -r /", True, None),
    ("a(){ a|a& }; a", True, None),
    # --- M488: actionable hints ---
    ('python3 -c "print(1)"', True, "script.py"),
    ('node -e "console.log(1)"', True, "script.js"),
    ("eval echo hello", True, "directly"),
    ("echo KEY=val > ~/.kiso/.env", True, "kiso env set"),
]


class TestCheckCommandDenyList:
    @pytest.mark.parametrize(
        "cmd,should_block,hint_contains",
        _DENY_LIST_CASES,
        ids=[c[0][:50] for c in _DENY_LIST_CASES],
    )
    def test_check_command_deny_list(self, cmd, should_block, hint_contains):
        result = check_command_deny_list(cmd)
        assert (result is not None) == should_block, f"cmd={cmd!r}"
        if hint_contains is not None:
            assert hint_contains in result, f"cmd={cmd!r} missing hint {hint_contains!r}"

    def test_no_hint_for_generic_deny(self):
        """patterns without hints don't include 'Hint:' in message."""
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
        """secrets with special chars get JSON-escaped variant."""
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
        """JSON-escaped secret variant is stripped from output."""
        secret = 'key\nwith"quotes'
        result = sanitize_output(
            '{"token": "key\\nwith\\"quotes"}',
            {"KEY": secret},
            {},
        )
        assert 'key\\nwith\\"quotes' not in result
        assert "[REDACTED]" in result

    def test_empty_secrets_noop(self):
        """no secrets → output returned unchanged (no regex compilation)."""
        result = sanitize_output("normal output", {}, {})
        assert result == "normal output"

    def test_cache_reused_on_same_secrets(self):
        """compiled pattern is cached across calls with same secrets."""
        import kiso.security as sec
        secrets = {"KEY": "sk-abc123xyz"}
        sanitize_output("first call", secrets, {})
        cached_pattern = sec._sanitize_cache[1]
        assert cached_pattern is not None
        sanitize_output("second call", secrets, {})
        assert sec._sanitize_cache[1] is cached_pattern  # same object

    def test_cache_invalidated_on_new_secrets(self):
        """cache is rebuilt when secret values change."""
        import kiso.security as sec
        sanitize_output("call 1", {"A": "secret-one-val"}, {})
        old_pattern = sec._sanitize_cache[1]
        sanitize_output("call 2", {"A": "secret-two-val"}, {})
        assert sec._sanitize_cache[1] is not old_pattern


class TestSanitizeValue:
    def test_sanitizes_nested_dict_and_list(self):
        result = sanitize_value(
            {
                "cmd": "echo sk-abc123xyz",
                "nested": [
                    "tok_12345",
                    {"url": "https://x.test/?token=tok_12345"},
                ],
            },
            {"KEY": "sk-abc123xyz"},
            {"TEMP": "tok_12345"},
        )
        assert result == {
            "cmd": "echo [REDACTED]",
            "nested": [
                "[REDACTED]",
                {"url": "https://x.test/?token=[REDACTED]"},
            ],
        }

    def test_preserves_non_string_values(self):
        result = sanitize_value(
            {"count": 3, "ok": True, "items": [1, None, False]},
            {"KEY": "secret"},
            {},
        )
        assert result == {"count": 3, "ok": True, "items": [1, None, False]}


class TestCollectDeploySecrets:
    def test_env_vars(self):
        env = {
            "KISO_WRAPPER_API_KEY": "sk-123",
            "KISO_CONNECTOR_TOKEN": "ct-456",
            "PATH": "/usr/bin",
            "HOME": "/home/user",
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets == {
            "KISO_WRAPPER_API_KEY": "sk-123",
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

    def test_accepts_no_arguments(self):
        """M66d: collect_deploy_secrets takes no parameters (config param removed)."""
        import inspect
        sig = inspect.signature(collect_deploy_secrets)
        assert len(sig.parameters) == 0, (
            "collect_deploy_secrets must have no parameters; "
            f"found: {list(sig.parameters)}"
        )

    def test_empty_env_returns_empty_dict(self):
        """No KISO_WRAPPER_*/KISO_CONNECTOR_* env vars → empty dict."""
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            secrets = collect_deploy_secrets()
        assert secrets == {}

    def test_non_kiso_prefixed_vars_excluded(self):
        """Non-kiso-prefixed vars must not appear in secrets."""
        env = {"MY_SECRET": "oops", "KISO_WRAPPER_X": "ok", "PATH": "/bin"}
        with patch.dict(os.environ, env, clear=True):
            secrets = collect_deploy_secrets()
        assert "MY_SECRET" not in secrets
        assert "KISO_WRAPPER_X" in secrets


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
        "bob": User(role="user", wrappers=["search", "deploy"]),
        "charlie": User(role="user", wrappers="*"),
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
        result = revalidate_permissions(cfg, "bob", "wrapper", wrapper_name="search")
        assert result.allowed is True

    def test_revalidate_skill_denied(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "wrapper", wrapper_name="forbidden")
        assert result.allowed is False
        assert "not in user's allowed wrappers" in result.reason

    def test_revalidate_admin_all_skills(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "alice", "wrapper", wrapper_name="anything")
        assert result.allowed is True

    def test_revalidate_no_username(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, None, "exec")
        assert result.allowed is True
        assert result.role == "admin"

    def test_revalidate_wildcard_skills(self):
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "charlie", "wrapper", wrapper_name="anything")
        assert result.allowed is True

    def test_revalidate_exec_allowed_for_user_role(self):
        """exec tasks allowed for user role (wrapper check only triggers for wrapper tasks)."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "exec")
        assert result.allowed is True
        assert result.role == "user"

    def test_revalidate_skill_name_none_skips_check(self):
        """wrapper_name=None with task_type='wrapper' skips wrapper-level check."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "wrapper", wrapper_name=None)
        assert result.allowed is True

    def test_revalidate_returns_tools_field(self):
        """PermissionResult.wrappers populated from user config."""
        cfg = _perm_config()
        result = revalidate_permissions(cfg, "bob", "exec")
        assert result.wrappers == ["search", "deploy"]

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
        assert result.wrappers == ["search", "deploy"]


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
