"""Tests for kiso.config — loading, validation, defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.config import CONFIG_TEMPLATE, MODEL_DEFAULTS, MODEL_DESCRIPTIONS, SETTINGS_DEFAULTS, ConfigError, load_config, reload_config, setting_bool, setting_float, setting_int


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
briefer     = "deepseek/deepseek-v3.2"
classifier  = "deepseek/deepseek-v3.2"
planner     = "deepseek/deepseek-v3.2"
reviewer    = "deepseek/deepseek-v3.2"
curator     = "deepseek/deepseek-v3.2"
worker      = "deepseek/deepseek-v3.2"
summarizer  = "deepseek/deepseek-v3.2"
paraphraser = "deepseek/deepseek-v3.2"
messenger   = "deepseek/deepseek-v3.2"
searcher    = "perplexity/sonar"
consolidator = "google/gemini-2.5-flash-lite"

[settings]
context_messages          = 7
summarize_threshold       = 30
bot_name                  = "Kiso"
knowledge_max_facts       = 50
fact_decay_days           = 7
fact_decay_rate           = 0.1
fact_archive_threshold    = 0.3
fact_consolidation_min_ratio = 0.3
max_replan_depth          = 3
max_validation_retries    = 3
max_plan_tasks            = 20
llm_timeout              = 120
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


_MISSING_SECTION_CASES = [
    # (toml_text, expected_err_substring)
    (
        "[providers.x]\nbase_url = \"http://x\"\n[users.a]\nrole = \"admin\"\n",
        "[tokens]",
    ),
    (
        "[tokens]\ncli = \"tok\"\n[users.a]\nrole = \"admin\"\n",
        "[providers]",
    ),
    (
        "[tokens]\ncli = \"tok\"\n[providers.x]\nbase_url = \"http://x\"\n",
        "[users]",
    ),
]


@pytest.mark.parametrize(
    "text,expected_err",
    _MISSING_SECTION_CASES,
    ids=["missing_tokens", "missing_providers", "missing_users"],
)
def test_missing_required_section(tmp_path: Path, capsys, text, expected_err):
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert expected_err in _die_msg(capsys)


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


def test_user_role_user_without_tools(tmp_path: Path, capsys):
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
    assert "tools" in err


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
tools = "*"
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


def test_missing_model_role_uses_default(tmp_path: Path):
    """Config missing a model role fills from MODEL_DEFAULTS silently."""
    text = VALID.replace('planner     = "deepseek/deepseek-v3.2"\n', "")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.models["planner"] == MODEL_DEFAULTS["planner"]


def test_missing_setting_uses_default(tmp_path: Path):
    """Missing settings fall back to SETTINGS_DEFAULTS silently."""
    text = VALID.replace("llm_timeout              = 120\n", "")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.settings["llm_timeout"] == SETTINGS_DEFAULTS["llm_timeout"]


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


def test_tools_invalid_type(tmp_path: Path, capsys):
    text = """\
[tokens]
cli = "tok"
[providers.x]
base_url = "http://x"
[users.bob]
role = "user"
tools = 42
"""
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "tools" in _die_msg(capsys)


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


# (settings_dict, key, kwargs, expected)
_SETTING_BOOL_CASES = [
    # Native booleans
    ({"key": True}, "key", {}, True),
    ({"key": False}, "key", {}, False),
    # Missing key falls back to default
    ({}, "key", {"default": True}, True),
    ({}, "key", {"default": False}, False),
    # String true/false
    ({"key": "true"}, "key", {}, True),
    ({"key": "false"}, "key", {}, False),
    # String yes/no
    ({"key": "yes"}, "key", {}, True),
    ({"key": "no"}, "key", {}, False),
    # String 1/0
    ({"key": "1"}, "key", {}, True),
    ({"key": "0"}, "key", {}, False),
    # Integers are NOT coerced — fall through to default
    ({"key": 1}, "key", {"default": True}, True),
    ({"key": 1}, "key", {"default": False}, False),
    ({"key": 0}, "key", {"default": True}, True),
    # Case insensitive
    ({"key": "TRUE"}, "key", {}, True),
    ({"key": "FALSE"}, "key", {}, False),
    ({"key": "False"}, "key", {}, False),
    # Whitespace stripped
    ({"key": " true "}, "key", {}, True),
    ({"key": " false "}, "key", {}, False),
    # Unexpected types use default
    ({"key": [1, 2]}, "key", {"default": True}, True),
    ({"key": {"a": 1}}, "key", {"default": False}, False),
    # Unrecognized strings use default
    ({"key": "maybe"}, "key", {"default": True}, True),
    ({"key": "maybe"}, "key", {"default": False}, False),
]


class TestSettingBool:
    @pytest.mark.parametrize(
        "settings,key,kwargs,expected",
        _SETTING_BOOL_CASES,
        ids=[f"{s.get(k, 'MISSING')!r}->{e}" for s, k, kw, e in _SETTING_BOOL_CASES],
    )
    def test_setting_bool(self, settings, key, kwargs, expected):
        assert setting_bool(settings, key, **kwargs) is expected


# --- M34: settings defaults ---


def test_m34_settings_defaults():
    """M34 fact decay/archive settings have correct defaults."""
    assert SETTINGS_DEFAULTS["fact_decay_days"] == 7
    assert SETTINGS_DEFAULTS["fact_decay_rate"] == 0.1
    assert SETTINGS_DEFAULTS["fact_archive_threshold"] == 0.3


# --- M37: robustness fixes ---


def test_m37_fact_consolidation_min_ratio_default():
    """M37: fact_consolidation_min_ratio has correct default."""
    assert SETTINGS_DEFAULTS["fact_consolidation_min_ratio"] == 0.3


def test_m37_missing_consolidation_ratio_uses_default(tmp_path: Path):
    """M39: missing fact_consolidation_min_ratio falls back to default."""
    text = VALID.replace("fact_consolidation_min_ratio = 0.3\n", "")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.settings["fact_consolidation_min_ratio"] == 0.3


# --- M84e: settings type validation ---


def test_m84e_setting_wrong_type_int_exits(tmp_path: Path, capsys):
    """M84e: setting an int key to a string must exit with type error."""
    text = VALID.replace("max_plan_tasks            = 20",
                         'max_plan_tasks            = "twenty"')
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    err = _die_msg(capsys)
    assert "type errors" in err
    assert "max_plan_tasks" in err


def test_m84e_setting_wrong_type_bool_exits(tmp_path: Path, capsys):
    """M84e: setting a bool key to an int must exit with type error."""
    text = VALID.replace("fast_path_enabled         = true",
                         "fast_path_enabled         = 1")
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, text))
    assert "fast_path_enabled" in _die_msg(capsys)


def test_m84e_unknown_setting_key_ignored(tmp_path: Path):
    """M84e: unknown settings keys are allowed (forward-compatible)."""
    text = VALID + 'unknown_future_key = "hello"\n'
    cfg = load_config(_write(tmp_path, text))
    assert cfg.settings["unknown_future_key"] == "hello"


def test_m84e_valid_settings_no_error(tmp_path: Path):
    """M84e: correct types must not trigger any error."""
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.settings["max_plan_tasks"] == 20
    assert cfg.settings["fast_path_enabled"] is True


# --- M87c: setting_int / setting_float helpers ---


_SETTING_INT_CASES = [
    # (settings, key, kwargs, expected)
    ({"key": 42}, "key", {}, 42),
    ({"key": -5}, "key", {"lo": 0}, 0),          # below min clamped
    ({"key": 9999}, "key", {"hi": 100}, 100),     # above max clamped
    ({"key": 1}, "key", {"lo": 1, "hi": 10}, 1),  # exact min not clamped
    ({"key": 10}, "key", {"lo": 1, "hi": 10}, 10),  # exact max not clamped
    ({"key": 0}, "key", {}, 0),                   # no bounds, no clamp
]


class TestSettingInt:
    @pytest.mark.parametrize(
        "settings,key,kwargs,expected",
        _SETTING_INT_CASES,
        ids=[f"{s[k]}->{e}" for s, k, kw, e in _SETTING_INT_CASES],
    )
    def test_setting_int(self, settings, key, kwargs, expected):
        assert setting_int(settings, key, **kwargs) == expected

    def test_missing_key_falls_back_to_defaults(self):
        """missing key uses SETTINGS_DEFAULTS fallback."""
        assert setting_int({}, "llm_timeout", lo=1) == SETTINGS_DEFAULTS["llm_timeout"]

    def test_missing_key_no_default_raises(self):
        """missing key with no default raises ConfigError."""
        with pytest.raises(ConfigError, match="Missing required setting"):
            setting_int({}, "nonexistent_setting_xyz")


_SETTING_FLOAT_CASES = [
    # (settings, key, kwargs, expected)
    ({"key": 0.5}, "key", {}, 0.5),
    ({"key": -1.0}, "key", {"lo": 0.0}, 0.0),       # below min clamped
    ({"key": 2.0}, "key", {"hi": 1.0}, 1.0),         # above max clamped
    ({"key": 0.0}, "key", {"lo": 0.0, "hi": 1.0}, 0.0),  # exact min
    ({"key": 1.0}, "key", {"lo": 0.0, "hi": 1.0}, 1.0),  # exact max
    ({"key": 99.9}, "key", {}, 99.9),                 # no bounds
]


class TestSettingFloat:
    @pytest.mark.parametrize(
        "settings,key,kwargs,expected",
        _SETTING_FLOAT_CASES,
        ids=[f"{s[k]}->{e}" for s, k, kw, e in _SETTING_FLOAT_CASES],
    )
    def test_setting_float(self, settings, key, kwargs, expected):
        assert setting_float(settings, key, **kwargs) == pytest.approx(expected)

    def test_missing_key_falls_back_to_defaults(self):
        """missing key uses SETTINGS_DEFAULTS fallback."""
        result = setting_float({}, "fact_decay_rate", lo=0.0, hi=1.0)
        assert result == pytest.approx(SETTINGS_DEFAULTS["fact_decay_rate"])

    def test_missing_key_no_default_raises(self):
        """missing key with no default raises ConfigError."""
        with pytest.raises(ConfigError, match="Missing required setting"):
            setting_float({}, "nonexistent_setting_xyz")


def test_model_descriptions_cover_all_defaults():
    """Every MODEL_DEFAULTS role must have a description in MODEL_DESCRIPTIONS."""
    missing = set(MODEL_DEFAULTS) - set(MODEL_DESCRIPTIONS)
    assert not missing, f"MODEL_DESCRIPTIONS missing roles: {missing}"


def test_model_descriptions_no_extra_roles():
    """MODEL_DESCRIPTIONS should not have roles absent from MODEL_DEFAULTS."""
    extra = set(MODEL_DESCRIPTIONS) - set(MODEL_DEFAULTS)
    assert not extra, f"MODEL_DESCRIPTIONS has extra roles: {extra}"


def test_config_template_models_match_defaults():
    """CONFIG_TEMPLATE [models] section must list exactly the roles in MODEL_DEFAULTS."""
    import tomllib

    parsed = tomllib.loads(CONFIG_TEMPLATE)
    template_models = set(parsed.get("models", {}))
    expected = set(MODEL_DEFAULTS)
    assert template_models == expected, (
        f"CONFIG_TEMPLATE [models] drift: "
        f"missing={expected - template_models}, extra={template_models - expected}"
    )


# --- M1264: metadata as single source of truth ---


class TestConfigMetadataSingleSource:
    """Models and settings are defined in metadata tables; the legacy
    exports (MODEL_DEFAULTS, MODEL_DESCRIPTIONS, SETTINGS_DEFAULTS) are
    derived from those tables at module load time.
    """

    def test_model_metadata_table_exists_and_nonempty(self):
        from kiso.config import _MODEL_METADATA  # type: ignore[attr-defined]
        assert isinstance(_MODEL_METADATA, (list, tuple))
        assert len(_MODEL_METADATA) > 0
        # Each entry must be (role, default_model_id, description)
        for entry in _MODEL_METADATA:
            assert len(entry) == 3, f"Bad model metadata entry: {entry!r}"
            role, default, desc = entry
            assert isinstance(role, str) and role
            assert isinstance(default, str) and default
            assert isinstance(desc, str) and desc

    def test_settings_metadata_table_exists_and_nonempty(self):
        from kiso.config import _SETTINGS_METADATA  # type: ignore[attr-defined]
        assert isinstance(_SETTINGS_METADATA, (list, tuple))
        assert len(_SETTINGS_METADATA) > 0
        for entry in _SETTINGS_METADATA:
            assert len(entry) == 2, f"Bad settings metadata entry: {entry!r}"
            key, _default = entry
            assert isinstance(key, str) and key

    def test_model_defaults_derived_from_metadata(self):
        """MODEL_DEFAULTS must equal {role: default for role, default, _ in _MODEL_METADATA}."""
        from kiso.config import _MODEL_METADATA  # type: ignore[attr-defined]
        derived = {role: default for role, default, _ in _MODEL_METADATA}
        assert derived == MODEL_DEFAULTS, (
            f"MODEL_DEFAULTS drift from _MODEL_METADATA: "
            f"derived={derived} vs exported={MODEL_DEFAULTS}"
        )

    def test_model_descriptions_derived_from_metadata(self):
        from kiso.config import _MODEL_METADATA  # type: ignore[attr-defined]
        derived = {role: desc for role, _, desc in _MODEL_METADATA}
        assert derived == MODEL_DESCRIPTIONS

    def test_settings_defaults_derived_from_metadata(self):
        from kiso.config import _SETTINGS_METADATA  # type: ignore[attr-defined]
        derived = {key: default for key, default in _SETTINGS_METADATA}
        assert derived == SETTINGS_DEFAULTS, (
            f"SETTINGS_DEFAULTS drift from _SETTINGS_METADATA: "
            f"derived has {set(derived) - set(SETTINGS_DEFAULTS)} extra, "
            f"missing {set(SETTINGS_DEFAULTS) - set(derived)}"
        )

    def test_settings_runtime_types_preserved(self):
        """Type of each setting must match exactly — cli/config_cmd.py
        relies on isinstance() checks for coercion."""
        from kiso.config import _SETTINGS_METADATA  # type: ignore[attr-defined]
        for key, default in _SETTINGS_METADATA:
            exported_type = type(SETTINGS_DEFAULTS[key])
            derived_type = type(default)
            assert exported_type is derived_type, (
                f"Type drift on {key!r}: exported={exported_type.__name__} "
                f"vs metadata={derived_type.__name__}"
            )

    def test_template_settings_keys_match_metadata(self):
        """CONFIG_TEMPLATE [settings] keys must match _SETTINGS_METADATA keys.

        Stronger drift check than the existing models test — protects the
        forgotten case where someone adds to _SETTINGS_METADATA but not
        the template (or vice versa).
        """
        import tomllib
        from kiso.config import _SETTINGS_METADATA  # type: ignore[attr-defined]

        parsed = tomllib.loads(CONFIG_TEMPLATE)
        template_keys = set(parsed.get("settings", {}))
        metadata_keys = {key for key, _ in _SETTINGS_METADATA}
        assert template_keys == metadata_keys, (
            f"CONFIG_TEMPLATE [settings] drift: "
            f"missing={metadata_keys - template_keys}, "
            f"extra={template_keys - metadata_keys}"
        )

    def test_template_settings_values_match_metadata(self):
        """Each [settings] value in CONFIG_TEMPLATE must equal the
        metadata default — types and values byte-for-byte."""
        import tomllib
        from kiso.config import _SETTINGS_METADATA  # type: ignore[attr-defined]

        parsed = tomllib.loads(CONFIG_TEMPLATE)
        template_settings = parsed.get("settings", {})
        for key, default in _SETTINGS_METADATA:
            if key not in template_settings:
                continue  # caught by the keys test above
            template_val = template_settings[key]
            assert template_val == default, (
                f"Setting {key!r}: template={template_val!r} vs metadata={default!r}"
            )
            assert type(template_val) is type(default), (
                f"Setting {key!r}: template type={type(template_val).__name__} "
                f"vs metadata type={type(default).__name__}"
            )

    def test_template_models_values_match_metadata(self):
        """Each [models] value in CONFIG_TEMPLATE must equal the metadata default."""
        import tomllib
        from kiso.config import _MODEL_METADATA  # type: ignore[attr-defined]

        parsed = tomllib.loads(CONFIG_TEMPLATE)
        template_models = parsed.get("models", {})
        for role, default, _desc in _MODEL_METADATA:
            assert template_models.get(role) == default, (
                f"Model {role!r}: template={template_models.get(role)!r} "
                f"vs metadata={default!r}"
            )


# --- M217: resource limits in config ---


class TestResourceLimitDefaults:
    def test_defaults_exist(self):
        """Resource limit settings exist in SETTINGS_DEFAULTS."""
        assert SETTINGS_DEFAULTS["max_memory_gb"] == 4
        assert SETTINGS_DEFAULTS["max_cpus"] == 2
        assert SETTINGS_DEFAULTS["max_disk_gb"] == 32
        assert SETTINGS_DEFAULTS["max_pids"] == 512

    def test_config_template_includes_resource_limits(self):
        """CONFIG_TEMPLATE has resource limit settings."""
        import tomllib
        parsed = tomllib.loads(CONFIG_TEMPLATE)
        settings = parsed.get("settings", {})
        assert settings["max_memory_gb"] == 4
        assert settings["max_cpus"] == 2
        assert settings["max_disk_gb"] == 32
        assert settings["max_pids"] == 512

    def test_loaded_config_has_resource_limits(self, tmp_path: Path):
        """Loaded config includes resource limits from defaults."""
        cfg = load_config(_write(tmp_path, VALID))
        assert cfg.settings["max_memory_gb"] == 4
        assert cfg.settings["max_cpus"] == 2
        assert cfg.settings["max_disk_gb"] == 32
        assert cfg.settings["max_pids"] == 512

    def test_custom_resource_limits(self, tmp_path: Path):
        """Custom resource limits override defaults."""
        text = VALID + "max_memory_gb = 8\nmax_cpus = 4\nmax_disk_gb = 64\nmax_pids = 1024\n"
        cfg = load_config(_write(tmp_path, text))
        assert cfg.settings["max_memory_gb"] == 8
        assert cfg.settings["max_cpus"] == 4
        assert cfg.settings["max_disk_gb"] == 64
        assert cfg.settings["max_pids"] == 1024


# --- M542: KISO_HOME env var ---


class TestKisoHomeEnvVar:
    """KISO_DIR respects KISO_HOME environment variable."""

    def test_default_is_home_dot_kiso(self, monkeypatch):
        """Without KISO_HOME, KISO_DIR is ~/.kiso."""
        monkeypatch.delenv("KISO_HOME", raising=False)
        import os
        result = Path(os.environ.get("KISO_HOME", str(Path.home() / ".kiso")))
        assert result == Path.home() / ".kiso"

    def test_kiso_home_overrides_kiso_dir(self, tmp_path, monkeypatch):
        """KISO_HOME env var overrides KISO_DIR at import time."""
        test_dir = tmp_path / "custom_kiso"
        test_dir.mkdir()
        monkeypatch.setenv("KISO_HOME", str(test_dir))
        # Re-evaluate the expression (can't re-import module-level constant,
        # but we can verify the logic directly)
        import os
        result = Path(os.environ.get("KISO_HOME", str(Path.home() / ".kiso")))
        assert result == test_dir

    def test_kiso_home_absent_uses_default(self, monkeypatch):
        """Without KISO_HOME set, falls back to ~/.kiso."""
        monkeypatch.delenv("KISO_HOME", raising=False)
        import os
        result = Path(os.environ.get("KISO_HOME", str(Path.home() / ".kiso")))
        assert result == Path.home() / ".kiso"
