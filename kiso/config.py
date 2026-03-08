"""Load and validate ~/.kiso/config.toml."""

from __future__ import annotations

import logging
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

KISO_DIR = Path.home() / ".kiso"
CONFIG_PATH = KISO_DIR / "config.toml"
LLM_API_KEY_ENV = "KISO_LLM_API_KEY"

NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Default values used to write config.toml on first run.
# NOT used as runtime fallbacks — all settings must be explicitly present.
SETTINGS_DEFAULTS: dict[str, int | float | str | bool | list] = {
    # conversation
    "context_messages": 5,
    "summarize_threshold": 30,
    "summarize_messages_limit": 100,
    "bot_name": "Kiso",
    # knowledge / memory
    "knowledge_max_facts": 50,
    "fact_decay_days": 7,
    "fact_decay_rate": 0.1,
    "fact_archive_threshold": 0.3,
    "fact_consolidation_min_ratio": 0.3,
    # planning
    "max_replan_depth": 5,
    "max_validation_retries": 3,
    "max_plan_tasks": 20,
    # execution
    "classifier_timeout": 30,
    "llm_timeout": 300,
    "planner_timeout": 300,
    "messenger_timeout": 120,
    "max_output_size": 1048576,
    "max_worker_retries": 2,
    # limits
    "max_memory_gb": 4,
    "max_cpus": 2,
    "max_disk_gb": 32,
    "max_pids": 512,
    "max_llm_calls_per_message": 200,
    "max_message_size": 65536,
    "max_queue_size": 50,
    # server
    "host": "0.0.0.0",
    "port": 8333,
    "worker_idle_timeout": 300,
    # fast path
    "fast_path_enabled": True,
    # briefer (context intelligence layer)
    "briefer_enabled": True,
    # webhooks
    "webhook_allow_list": [],
    "webhook_require_https": True,
    "webhook_secret": "",
    "webhook_max_payload": 1048576,
}

MODEL_DEFAULTS: dict[str, str] = {
    "briefer": "google/gemini-2.5-flash-lite",
    "classifier": "google/gemini-2.5-flash-lite",
    "planner": "z-ai/glm-4.7",
    "reviewer": "google/gemini-2.5-flash-lite",
    "curator": "google/gemini-2.5-flash-lite",
    "worker": "google/gemini-2.5-flash-lite",
    "summarizer": "google/gemini-2.5-flash-lite",
    "paraphraser": "google/gemini-2.5-flash-lite",
    "messenger": "qwen/qwen3.5-flash-02-23",
    "searcher": "perplexity/sonar",
}

# M271: Per-role reasoning config sent to OpenRouter.  Roles not listed here
# (or mapped to None) get no reasoning parameter — the provider's default applies.
# Valid effort levels: "minimal", "low", "medium", "high".
REASONING_DEFAULTS: dict[str, dict | None] = {
    "messenger": {"effort": "low"},
}

# Descriptions shown during interactive install. Keyed by role name.
MODEL_DESCRIPTIONS: dict[str, str] = {
    "briefer": "selects relevant context for each LLM role",
    "classifier": "classifies messages as plan or chat",
    "planner": "interprets requests, creates task plans",
    "reviewer": "checks task output, decides replan",
    "curator": "manages learned knowledge",
    "worker": "translates tasks to shell commands",
    "summarizer": "compresses conversation history",
    "paraphraser": "prompt injection defense",
    "messenger": "writes human-readable responses",
    "searcher": "web search (native search)",
}

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
briefer     = "google/gemini-2.5-flash-lite"  # context selection (150 t/s, cheapest)
classifier  = "google/gemini-2.5-flash-lite"  # message classification (fast, simple)
planner     = "z-ai/glm-4.7"                   # plan generation (MMLU 83, 130 t/s)
reviewer    = "google/gemini-2.5-flash-lite"   # output review (1.8s, json_schema native)
curator     = "google/gemini-2.5-flash-lite"   # knowledge curation (simple classification)
worker      = "google/gemini-2.5-flash-lite"   # command translation (1.3s, cheapest)
summarizer  = "google/gemini-2.5-flash-lite"   # conversation summary (async, cheap)
paraphraser = "google/gemini-2.5-flash-lite"   # prompt injection defense (critical path)
messenger   = "qwen/qwen3.5-flash-02-23"            # user-facing responses (MMLU 82, natural)
searcher    = "perplexity/sonar"               # web search (native search API)

[settings]
# --- conversation ---
context_messages          = 5        # recent messages sent to planner
summarize_threshold       = 30       # message count before summarizer runs
summarize_messages_limit  = 100      # max messages sent to summarizer LLM per run
bot_name                  = "Kiso"

# --- knowledge / memory ---
knowledge_max_facts       = 50
fact_decay_days           = 7
fact_decay_rate           = 0.1
fact_archive_threshold    = 0.3
fact_consolidation_min_ratio = 0.3  # abort consolidation if fewer than this fraction survive

# --- planning ---
max_replan_depth          = 5
max_validation_retries    = 3
max_plan_tasks            = 20

# --- execution ---
classifier_timeout        = 30       # seconds for classifier LLM call; falls back to planner on timeout
llm_timeout               = 300      # seconds; timeout for post-plan LLM calls (curator, summarizer)
planner_timeout           = 300      # seconds for planner LLM calls (higher for reasoning models)
messenger_timeout         = 120      # seconds for messenger LLM calls (fast-path + msg tasks)
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
worker_idle_timeout       = 300

# --- fast path ---
fast_path_enabled         = true     # skip planner for conversational messages

# --- briefer (context intelligence layer) ---
briefer_enabled           = true     # LLM-based context selection for each pipeline stage

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
    skills: str | list[str] | None = None  # None for admin, "*" or list for user
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


def setting_int(settings: dict, key: str, *, lo: int | None = None, hi: int | None = None) -> int:
    """Read an integer setting, clamping to [lo, hi] with a warning if out of range."""
    val = int(settings[key])
    if lo is not None and val < lo:
        log.warning("Setting %s=%d is below minimum %d, clamping to %d", key, val, lo, lo)
        val = lo
    if hi is not None and val > hi:
        log.warning("Setting %s=%d is above maximum %d, clamping to %d", key, val, hi, hi)
        val = hi
    return val


def setting_float(settings: dict, key: str, *, lo: float | None = None, hi: float | None = None) -> float:
    """Read a float setting, clamping to [lo, hi] with a warning if out of range."""
    val = float(settings[key])
    if lo is not None and val < lo:
        log.warning("Setting %s=%f is below minimum %f, clamping to %f", key, val, lo, lo)
        val = lo
    if hi is not None and val > hi:
        log.warning("Setting %s=%f is above maximum %f, clamping to %f", key, val, hi, hi)
        val = hi
    return val


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

        # skills
        skills = udata.get("skills")
        if role == "user":
            if skills is None:
                on_error(f"user '{uname}' has role=user but no 'skills' field")
            if skills != "*" and not isinstance(skills, list):
                on_error(f"user '{uname}': skills must be '*' or a list of skill names")

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

        users[uname] = User(role=role, skills=skills, aliases=aliases)

    # --- models: all roles required ---
    models_raw = raw.get("models", {})
    missing_models = sorted(set(MODEL_DEFAULTS) - set(models_raw))
    if missing_models:
        on_error(
            f"[models] missing required fields: {', '.join(missing_models)}\n"
            f"  Add them to [models] in {path}"
        )
    models = dict(models_raw)

    # --- settings: start from defaults, override with config values ---
    settings_raw = raw.get("settings", {})
    # Backward compat: map old exec_timeout → llm_timeout
    if "exec_timeout" in settings_raw and "llm_timeout" not in settings_raw:
        settings_raw["llm_timeout"] = settings_raw.pop("exec_timeout")
    elif "exec_timeout" in settings_raw:
        del settings_raw["exec_timeout"]
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
