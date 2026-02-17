"""Tests for kiso.config — loading, validation, defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.config import MODEL_DEFAULTS, SETTINGS_DEFAULTS, ConfigError, load_config, reload_config


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


def test_defaults_applied(tmp_path: Path):
    cfg = load_config(_write(tmp_path, VALID))
    for key, val in SETTINGS_DEFAULTS.items():
        assert cfg.settings[key] == val
    for key, val in MODEL_DEFAULTS.items():
        assert cfg.models[key] == val


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


def test_config_file_not_found(tmp_path: Path, capsys):
    with pytest.raises(SystemExit):
        load_config(tmp_path / "nonexistent.toml")
    assert "not found" in _die_msg(capsys)


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
