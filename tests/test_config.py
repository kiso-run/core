"""Tests for kiso.config — loading, validation, defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.config import MODEL_DEFAULTS, SETTINGS_DEFAULTS, ConfigError, load_config, reload_config, setting_bool


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def _die_msg(capsys) -> str:
    """Return the stderr output after a SystemExit."""
    return capsys.readouterr().err


VALID = """\
[tokens]
cli = "tok"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[users.marco]
role = "admin"

[models]
planner     = "minimax/minimax-m2.5"
reviewer    = "deepseek/deepseek-v3.2"
curator     = "deepseek/deepseek-v3.2"
worker      = "deepseek/deepseek-v3.2"
summarizer  = "deepseek/deepseek-v3.2"
paraphraser = "deepseek/deepseek-v3.2"
messenger   = "deepseek/deepseek-v3.2"
searcher    = "perplexity/sonar"

[settings]
context_messages          = 7
summarize_threshold       = 30
bot_name                  = "Kiso"
knowledge_max_facts       = 50
fact_decay_days           = 7
fact_decay_rate           = 0.1
fact_archive_threshold    = 0.3
max_replan_depth          = 3
max_validation_retries    = 3
max_plan_tasks            = 20
exec_timeout              = 120
planner_timeout           = 60
max_output_size           = 1048576
max_worker_retries        = 1
max_llm_calls_per_message = 200
max_message_size          = 65536
max_queue_size            = 50
host                      = "0.0.0.0"
port                      = 8333
worker_idle_timeout       = 300
fast_path_enabled         = true
webhook_allow_list        = []
webhook_require_https     = true
webhook_secret            = ""
webhook_max_payload       = 1048576
"""


def test_load_valid_config(tmp_path: Path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.tokens == {"cli": "tok"}
    assert "openrouter" in cfg.providers
    assert cfg.users["marco"].role == "admin"


def test_missing_tokens(tmp_path: Path, capsys):
    text = """\
[providers.x]
base_url = "http://x"
[users.a]
role = "admin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "[tokens]" in _die_msg(capsys)


def test_missing_providers(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[users.a]
role = "admin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "[providers]" in _die_msg(capsys)


def test_missing_users(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "[users]" in _die_msg(capsys)


def test_invalid_username(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users.InvalidName]
role = "admin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "InvalidName" in _die_msg(capsys)


def test_invalid_token_name(tmp_path: Path, capsys):
    text = """\
[tokens]
BADTOKEN = "tok"
[providers.x]
base_url = "http://x"
[users.a]
role = "admin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "BADTOKEN" in _die_msg(capsys)


def test_user_role_user_without_skills(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users.bob]
role = "user"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    err = _die_msg(capsys)
    assert "bob" in err
    assert "skills" in err


def test_duplicate_alias(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users.alice]
role = "admin"
[users.alice.aliases]
discord = "alice123"
[users.bob]
role = "user"
skills = "*"
[users.bob.aliases]
discord = "alice123"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "duplicate alias" in _die_msg(capsys)


def test_all_settings_loaded(tmp_path: Path):
    """All settings from TOML are present — no silent code defaults."""
    cfg = load_config(_write(tmp_path, VALID))
    for key in SETTINGS_DEFAULTS:
        assert key in cfg.settings, f"Missing setting: {key}"
    for key in MODEL_DEFAULTS:
        assert key in cfg.models, f"Missing model: {key}"


def test_missing_model_role(tmp_path: Path, capsys):
    """Config missing a model role fails loudly."""
    text = VALID.replace('planner     = "minimax/minimax-m2.5"\n', "")
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "planner" in _die_msg(capsys)


def test_missing_setting(tmp_path: Path, capsys):
    """Config missing a setting fails loudly."""
    text = VALID.replace("exec_timeout              = 120\n", "")
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "exec_timeout" in _die_msg(capsys)


def test_provider_missing_base_url(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
api_key_env = "X"
[users.a]
role = "admin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "base_url" in _die_msg(capsys)


def test_config_file_not_found_writes_template(tmp_path: Path, capsys):
    """On first run (no config file), load_config writes the template and exits."""
    p = tmp_path / "config.toml"
    assert not p.exists()
    with pytest.raises(SystemExit) as exc_info:
        load_config(p)
    assert exc_info.value.code == 0
    assert p.exists(), "Template should be written"
    content = p.read_text()
    assert "[settings]" in content
    assert "[models]" in content
    assert "Config created" in capsys.readouterr().err


def test_token_not_string(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = 123
[providers.x]
base_url = "http://x"
[users.a]
role = "admin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "non-empty string" in _die_msg(capsys)


def test_provider_not_a_table(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers]
x = "not a table"
[users.a]
role = "admin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "must be a table" in _die_msg(capsys)


def test_user_not_a_table(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users]
a = "not a table"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "must be a table" in _die_msg(capsys)


def test_invalid_role(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users.a]
role = "superadmin"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    err = _die_msg(capsys)
    assert "superadmin" in err


def test_skills_invalid_type(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users.bob]
role = "user"
skills = 42
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "skills" in _die_msg(capsys)


def test_aliases_not_a_table(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users.a]
role = "admin"
aliases = "bad"
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "aliases" in _die_msg(capsys)


# --- reload_config ---


def test_reload_config_success(tmp_path: Path):
    cfg = reload_config(_write(tmp_path, VALID))
    assert cfg.tokens == {"cli": "tok"}
    assert "openrouter" in cfg.providers
    assert cfg.users["marco"].role == "admin"


def test_reload_config_raises_on_error(tmp_path: Path):
    text = """\
[providers.x]
base_url = "http://x"
[users.a]
role = "admin"
"""
    with pytest.raises(ConfigError, match=r"\[tokens\]"):
        reload_config(_write(tmp_path, text))


def test_reload_config_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        reload_config(tmp_path / "nonexistent.toml")


def test_sandbox_settings_removed():
    """Per-session sandbox replaced global sandbox_enabled/sandbox_user settings."""
    assert "sandbox_enabled" not in SETTINGS_DEFAULTS
    assert "sandbox_user" not in SETTINGS_DEFAULTS


# --- Malformed TOML / permission errors ---


def test_malformed_toml_clear_error(tmp_path: Path, capsys):
    """Malformed TOML on load_config gives a clear SystemExit message."""
    p = tmp_path / "config.toml"
    p.write_text("this is not [valid toml }{")
    with pytest.raises(SystemExit):
        load_config(p)
    err = _die_msg(capsys)
    assert "Malformed TOML" in err


def test_malformed_toml_reload_raises(tmp_path: Path):
    """Malformed TOML on reload_config raises ConfigError."""
    p = tmp_path / "config.toml"
    p.write_text("not valid [[ toml {{")
    with pytest.raises(ConfigError, match="Malformed TOML"):
        reload_config(p)


def test_config_permission_error(tmp_path: Path, capsys):
    """Unreadable config file gives a clear SystemExit message."""
    import os
    p = tmp_path / "config.toml"
    p.write_text(VALID)
    os.chmod(p, 0o000)
    try:
        with pytest.raises(SystemExit):
            load_config(p)
        err = _die_msg(capsys)
        assert "Cannot read" in err
    finally:
        os.chmod(p, 0o644)


# --- setting_bool ---


class TestSettingBool:
    def test_true_bool(self):
        assert setting_bool({"key": True}, "key") is True

    def test_false_bool(self):
        assert setting_bool({"key": False}, "key") is False

    def test_default_when_missing(self):
        assert setting_bool({}, "key", default=True) is True
        assert setting_bool({}, "key", default=False) is False

    def test_string_true(self):
        assert setting_bool({"key": "true"}, "key") is True

    def test_string_false(self):
        """String 'false' must NOT be truthy — this is the bug this helper fixes."""
        assert setting_bool({"key": "false"}, "key") is False

    def test_string_yes_no(self):
        assert setting_bool({"key": "yes"}, "key") is True
        assert setting_bool({"key": "no"}, "key") is False

    def test_string_1_0(self):
        assert setting_bool({"key": "1"}, "key") is True
        assert setting_bool({"key": "0"}, "key") is False

    def test_int_truthy(self):
        assert setting_bool({"key": 1}, "key") is True
        assert setting_bool({"key": 0}, "key") is False

    def test_string_case_insensitive(self):
        assert setting_bool({"key": "TRUE"}, "key") is True
        assert setting_bool({"key": "FALSE"}, "key") is False
        assert setting_bool({"key": "False"}, "key") is False

    def test_string_whitespace(self):
        assert setting_bool({"key": " true "}, "key") is True
        assert setting_bool({"key": " false "}, "key") is False

    def test_unexpected_type_uses_default(self):
        assert setting_bool({"key": [1, 2]}, "key", default=True) is True
        assert setting_bool({"key": {"a": 1}}, "key", default=False) is False

    def test_unrecognized_string_uses_default(self):
        assert setting_bool({"key": "maybe"}, "key", default=True) is True
        assert setting_bool({"key": "maybe"}, "key", default=False) is False


# --- M34: settings defaults ---


def test_m34_settings_defaults():
    """M34 fact decay/archive settings have correct defaults."""
    assert SETTINGS_DEFAULTS["fact_decay_days"] == 7
    assert SETTINGS_DEFAULTS["fact_decay_rate"] == 0.1
    assert SETTINGS_DEFAULTS["fact_archive_threshold"] == 0.3
