"""Load and validate ~/.kiso/config.toml."""

from __future__ import annotations

import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

KISO_DIR = Path(os.environ.get("KISO_HOME", str(Path.home() / ".kiso")))
CONFIG_PATH = KISO_DIR / "config.toml"
LLM_API_KEY_ENV = "KISO_LLM_API_KEY"

NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Settings shown to the planner in system environment.
# Only user-facing settings that the planner might need to change.
USER_FACING_SETTINGS: tuple[str, ...] = (
    "bot_name",
    "bot_persona",
    "context_messages",
    "summarize_threshold",
    "knowledge_max_facts",
    "max_replan_depth",
    "consolidation_enabled",
    "consolidation_interval_hours",
)

# --- Single source of truth for settings and models ---------------------
#
# `_SETTINGS_METADATA` and `_MODEL_METADATA` are the canonical tables.
# `SETTINGS_DEFAULTS`, `MODEL_DEFAULTS`, and `MODEL_DESCRIPTIONS` are
# derived from them at module load time and remain importable for
# backward compatibility.
#
# To add or modify a setting/model, edit ONLY the metadata table and
# (if user-visible) the corresponding line in CONFIG_TEMPLATE below.
# `tests/test_config.py::TestConfigMetadataSingleSource` enforces that
# CONFIG_TEMPLATE stays in sync.
#
# Each metadata entry is a plain tuple — kept compact intentionally so
# the table reads like a config file rather than a class hierarchy.

# Settings: (key, default_value). Order is preserved in SETTINGS_DEFAULTS.
# NOT used as runtime fallbacks — all settings must be explicitly present
# in the loaded config; defaults are only used to fill missing keys at
# load time and to write the initial config.toml.
_SETTINGS_METADATA: tuple[tuple[str, int | float | str | bool | list], ...] = (
    # conversation
    ("context_messages", 5),
    ("summarize_threshold", 30),
    ("summarize_messages_limit", 100),
    ("bot_name", "Kiso"),
    ("bot_persona", "a friendly and knowledgeable assistant"),
    # knowledge / memory
    ("knowledge_max_facts", 50),
    ("fact_decay_days", 7),
    ("fact_decay_rate", 0.1),
    ("fact_archive_threshold", 0.3),
    ("fact_consolidation_min_ratio", 0.3),
    # consolidator (periodic knowledge quality review)
    ("consolidation_enabled", True),
    ("consolidation_interval_hours", 24),
    ("consolidation_min_facts", 20),
    # planning
    ("max_replan_depth", 5),
    ("max_validation_retries", 3),
    ("max_llm_retries", 3),
    ("max_plan_tasks", 20),
    ("planner_fallback_model", "minimax/minimax-m2.7"),
    # execution
    ("classifier_timeout", 30),
    ("llm_timeout", 600),
    ("stall_timeout", 60),
    ("max_output_size", 1048576),
    ("max_worker_retries", 2),
    # limits
    ("max_memory_gb", 4),
    ("max_cpus", 2),
    ("max_disk_gb", 32),
    ("max_pids", 512),
    ("max_llm_calls_per_message", 200),
    ("max_message_size", 65536),
    ("max_queue_size", 50),
    # server
    ("host", "0.0.0.0"),
    ("port", 8333),
    ("external_url", ""),
    ("worker_idle_timeout", 300),
    # fast path
    ("fast_path_enabled", True),
    # briefer (context intelligence layer)
    ("briefer_enabled", True),
    ("briefer_wrapper_filter_threshold", 10),
    # webhooks
    ("webhook_allow_list", []),
    ("webhook_require_https", True),
    ("webhook_secret", ""),
    ("webhook_max_payload", 1048576),
)

# Models: (role, default_model_id, description). Order is preserved in
# MODEL_DEFAULTS / MODEL_DESCRIPTIONS. Description is shown during
# interactive install.
_MODEL_METADATA: tuple[tuple[str, str, str], ...] = (
    ("briefer",      "google/gemini-2.5-flash",      "selects relevant context for each LLM role"),
    ("classifier",   "google/gemini-2.5-flash",      "classifies messages as plan or chat"),
    ("planner",      "deepseek/deepseek-v3.2",       "interprets requests, creates task plans"),
    ("reviewer",     "google/gemini-2.5-flash-lite", "checks task output, decides replan"),
    ("curator",      "google/gemini-2.5-flash",      "manages learned knowledge"),
    ("worker",       "deepseek/deepseek-v3.2",       "translates tasks to shell commands"),
    ("summarizer",   "google/gemini-2.5-flash-lite", "compresses conversation history"),
    ("paraphraser",  "google/gemini-2.5-flash-lite", "prompt injection defense"),
    ("messenger",    "deepseek/deepseek-v3.2",       "writes human-readable responses"),
    ("searcher",     "perplexity/sonar",             "web search (native search)"),
    ("consolidator", "google/gemini-2.5-flash-lite", "periodic knowledge quality review"),
)

# Derived legacy exports — preserved for backward compatibility.
SETTINGS_DEFAULTS: dict[str, int | float | str | bool | list] = {
    key: default for key, default in _SETTINGS_METADATA
}
MODEL_DEFAULTS: dict[str, str] = {
    role: default for role, default, _desc in _MODEL_METADATA
}
MODEL_DESCRIPTIONS: dict[str, str] = {
    role: desc for role, _default, desc in _MODEL_METADATA
}

# Per-role reasoning config sent to OpenRouter.  Roles not listed here
# (or mapped to None) get no reasoning parameter — the provider's default applies.
# Valid effort levels: "minimal", "low", "medium", "high".
REASONING_DEFAULTS: dict[str, dict | None] = {}

# Only the classifier needs a max_tokens cap (single-word response).
# All other roles rely on the model's native limit — removing artificial
# caps prevents silent truncation of complex outputs.
CLASSIFIER_MAX_TOKENS = 10

# Complete config.toml written on first run. Edit to configure your instance.
CONFIG_TEMPLATE = """\
# kiso configuration
# Documentation: https://github.com/kiso-run/core/blob/main/docs/config.md
# Generate tokens with: openssl rand -hex 32

[tokens]
# cli = "your-secret-token-here"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

# [providers.ollama]
# base_url = "http://localhost:11434/v1"

[users.admin]
role = "admin"
# aliases.discord = "YourDiscordUser#1234"

[models]
# Format: "provider/model-name" — all route through your gateway (e.g., OpenRouter)
# See docs/model-selection.md for rationale and alternatives
briefer     = "google/gemini-2.5-flash"       # context selection (json_schema native)
classifier  = "google/gemini-2.5-flash"       # message classification (fast, simple)
planner     = "deepseek/deepseek-v3.2"        # plan generation (fast, structured output)
reviewer    = "google/gemini-2.5-flash-lite"  # output review (json_schema native, high frequency)
curator     = "google/gemini-2.5-flash"       # knowledge curation (needs reliable json_schema)
worker      = "deepseek/deepseek-v3.2"        # command translation (strict output format)
summarizer  = "google/gemini-2.5-flash-lite"  # conversation summary (async, cheap)
paraphraser = "google/gemini-2.5-flash-lite"  # prompt injection defense (critical path)
messenger   = "deepseek/deepseek-v3.2"        # user-facing responses (natural language)
searcher    = "perplexity/sonar"              # web search (native search API)
consolidator = "google/gemini-2.5-flash-lite" # periodic knowledge quality review (async, cheap)

[settings]
# --- conversation ---
context_messages          = 5        # recent messages sent to planner
summarize_threshold       = 30       # message count before summarizer runs
summarize_messages_limit  = 100      # max messages sent to summarizer LLM per run
bot_name                  = "Kiso"
bot_persona               = "a friendly and knowledgeable assistant"

# --- knowledge / memory ---
knowledge_max_facts       = 50
fact_decay_days           = 7
fact_decay_rate           = 0.1
fact_archive_threshold    = 0.3
fact_consolidation_min_ratio = 0.3  # abort consolidation if fewer than this fraction survive
consolidation_enabled             = true    # periodic holistic knowledge review
consolidation_interval_hours      = 24      # minimum hours between consolidation runs
consolidation_min_facts           = 20      # minimum facts to trigger a consolidation

# --- planning ---
max_replan_depth          = 5
max_validation_retries    = 3
max_llm_retries           = 3        # retries for LLM-level failures (timeout, stall, HTTP errors)
max_plan_tasks            = 20
planner_fallback_model    = "minimax/minimax-m2.7"  # secondary model used when the primary planner times out / stalls

# --- execution ---
classifier_timeout        = 30       # seconds for classifier LLM call; falls back to planner on timeout
llm_timeout               = 600      # seconds; hard timeout for all LLM calls
stall_timeout             = 60       # seconds; abort streaming if no chunk arrives within this window
max_output_size           = 1048576  # max chars per task output (0 = unlimited)
max_worker_retries        = 2

# --- resource limits ---
max_memory_gb             = 4          # container RAM limit (applied via docker run/update)
max_cpus                  = 2          # container CPU limit (applied via docker run/update)
max_disk_gb               = 32         # app-level disk limit (applied immediately)
max_pids                  = 512        # container PID limit (applied via docker run/update)

# --- limits ---
max_llm_calls_per_message = 200
max_message_size          = 65536    # bytes, POST /msg content
max_queue_size            = 50       # queued messages per session

# --- server ---
host                      = "0.0.0.0"
port                      = 8333
external_url              = ""       # public base URL for webhook callbacks (empty = derive from host:port)
worker_idle_timeout       = 300

# --- fast path ---
fast_path_enabled         = true     # skip planner for conversational messages

# --- briefer (context intelligence layer) ---
briefer_enabled           = true     # LLM-based context selection for each pipeline stage
briefer_wrapper_filter_threshold = 10   # only invoke the briefer's wrapper-filtering pass when this many tools are available

# --- webhooks (only needed when using connector integrations) ---
webhook_allow_list        = []       # IPs exempt from SSRF check
webhook_require_https     = true
webhook_secret            = ""       # HMAC-SHA256 secret; empty = no signing
webhook_max_payload       = 1048576
"""


@dataclass(frozen=True)
class Provider:
    base_url: str


@dataclass(frozen=True)
class User:
    role: str  # "admin" | "user"
    wrappers: str | list[str] | None = None  # None for admin, "*" or list for user
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    tokens: dict[str, str]
    providers: dict[str, Provider]
    users: dict[str, User]
    models: dict[str, str]
    settings: dict[str, int | float | str | list[str]]
    raw: dict  # full parsed TOML for future use


class ConfigError(Exception):
    """Raised when config is invalid (for runtime reload)."""


def setting_bool(settings: dict, key: str, default: bool = False) -> bool:
    """Read a boolean setting with strict type handling.

    TOML natively parses ``true``/``false`` as Python ``bool``.
    If a user accidentally quotes the value (``"false"``), a plain
    truthiness check would treat the string as ``True``.  This helper
    rejects non-boolean types so the misconfiguration is caught early.
    """
    val = settings.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    # Anything else (int, list, dict …) — fall through to default
    return default


def _clamp_setting(settings: dict, key: str, type_fn, lo=None, hi=None):
    """Read a setting, convert with *type_fn*, clamp to [lo, hi]."""
    raw = settings.get(key, SETTINGS_DEFAULTS.get(key))
    if raw is None:
        raise ConfigError(f"Missing required setting: {key}")
    val = type_fn(raw)
    if lo is not None and val < lo:
        log.warning("Setting %s=%s below minimum %s, clamping", key, val, lo)
        val = lo
    if hi is not None and val > hi:
        log.warning("Setting %s=%s above maximum %s, clamping", key, val, hi)
        val = hi
    return val


def setting_int(settings: dict, key: str, *, lo: int | None = None, hi: int | None = None) -> int:
    """Read an integer setting, clamping to [lo, hi] with a warning."""
    return _clamp_setting(settings, key, int, lo, hi)


def setting_float(settings: dict, key: str, *, lo: float | None = None, hi: float | None = None) -> float:
    """Read a float setting, clamping to [lo, hi] with a warning."""
    return _clamp_setting(settings, key, float, lo, hi)


def _die(msg: str) -> None:
    print(f"config error: {msg}", file=sys.stderr)
    sys.exit(1)


def _build_config(path: Path, on_error) -> Config:
    """Core config builder. Calls on_error(msg) on validation failure.

    Handles malformed TOML (``TOMLDecodeError``) and file-system errors
    (``PermissionError``, ``OSError``) with clear messages via *on_error*.
    """

    def _check_name(name: str, kind: str) -> None:
        if not NAME_RE.match(name):
            on_error(f"{kind} '{name}' does not match {NAME_RE.pattern}")

    if not path.exists():
        on_error(f"{path} not found")

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        on_error(f"Malformed TOML in {path}: {e}")
    except (PermissionError, OSError) as e:
        on_error(f"Cannot read {path}: {e}")

    # --- required sections ---
    for section in ("tokens", "providers", "users"):
        if section not in raw or not raw[section]:
            on_error(f"[{section}] section is missing or empty")

    # --- tokens ---
    tokens: dict[str, str] = {}
    for name, value in raw["tokens"].items():
        _check_name(name, "token name")
        if not isinstance(value, str) or not value:
            on_error(f"token '{name}' must be a non-empty string")
        tokens[name] = value

    # --- providers ---
    providers: dict[str, Provider] = {}
    for name, prov in raw["providers"].items():
        _check_name(name, "provider name")
        if not isinstance(prov, dict):
            on_error(f"provider '{name}' must be a table")
        if "base_url" not in prov:
            on_error(f"provider '{name}' is missing base_url")
        providers[name] = Provider(
            base_url=prov["base_url"],
        )

    # --- users ---
    users: dict[str, User] = {}
    all_aliases: dict[str, str] = {}  # "connector:platform_id" → username

    for uname, udata in raw["users"].items():
        _check_name(uname, "username")
        if not isinstance(udata, dict):
            on_error(f"user '{uname}' must be a table")

        role = udata.get("role")
        if role not in ("admin", "user"):
            on_error(f"user '{uname}': role must be 'admin' or 'user', got '{role}'")

        wrappers = udata.get("wrappers")
        if role == "user":
            if wrappers is None:
                on_error(f"user '{uname}' has role=user but no 'wrappers' field")
            if wrappers != "*" and not isinstance(wrappers, list):
                on_error(f"user '{uname}': wrappers must be '*' or a list of wrapper names")

        # aliases
        aliases_raw = udata.get("aliases", {})
        if not isinstance(aliases_raw, dict):
            on_error(f"user '{uname}': aliases must be a table")
        aliases: dict[str, str] = {}
        for connector, platform_id in aliases_raw.items():
            key = f"{connector}:{platform_id}"
            if key in all_aliases:
                on_error(
                    f"duplicate alias: {connector}={platform_id} used by both "
                    f"'{all_aliases[key]}' and '{uname}'"
                )
            all_aliases[key] = uname
            aliases[connector] = platform_id

        users[uname] = User(role=role, wrappers=wrappers, aliases=aliases)

    # --- models: all roles required ---
    models_raw = raw.get("models", {})

    # Fill any missing model roles from defaults (so old configs automatically
    # gain new roles like 'consolidator' without manual edits).
    for role, default_model in MODEL_DEFAULTS.items():
        if role not in models_raw:
            models_raw[role] = default_model
    models = dict(models_raw)

    # --- settings: start from defaults, override with config values ---
    settings_raw = raw.get("settings", {})
    settings = dict(SETTINGS_DEFAULTS)
    settings.update(settings_raw)

    # Validate that overridden settings have the correct type
    type_errors: list[str] = []
    for key in settings_raw.keys() & SETTINGS_DEFAULTS.keys():
        val = settings_raw[key]
        default = SETTINGS_DEFAULTS[key]
        # bool is a subclass of int — check it first to avoid false positives
        if isinstance(default, bool):
            if not isinstance(val, bool):
                type_errors.append(f"{key}: expected bool, got {type(val).__name__}")
        elif not isinstance(val, type(default)):
            type_errors.append(f"{key}: expected {type(default).__name__}, got {type(val).__name__}")
    if type_errors:
        on_error(
            f"[settings] type errors in {path}:\n"
            + "\n".join(f"  {e}" for e in type_errors)
        )

    return Config(
        tokens=tokens,
        providers=providers,
        users=users,
        models=models,
        settings=settings,
        raw=raw,
    )


def load_config(path: Path | None = None) -> Config:
    """Load and validate config. Exits on error.

    On first run (config file absent): writes CONFIG_TEMPLATE and exits with
    instructions so the user knows where to fill in tokens and users.
    """
    target = path or CONFIG_PATH
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(
            f"Config created at {target}\n"
            f"  1. Set your token in [tokens]\n"
            f"  2. Configure [providers] and [users]\n"
            f"  3. Restart kiso",
            file=sys.stderr,
        )
        sys.exit(0)
    return _build_config(target, _die)


def reload_config(path: Path | None = None) -> Config:
    """Reload config at runtime. Raises ConfigError on failure."""
    def _raise(msg: str) -> None:
        raise ConfigError(msg)
    return _build_config(path or CONFIG_PATH, _raise)
