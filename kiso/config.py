"""Load and validate ~/.kiso/config.toml."""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KISO_DIR = Path.home() / ".kiso"
CONFIG_PATH = KISO_DIR / "config.toml"
LLM_API_KEY_ENV = "KISO_LLM_API_KEY"

NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

SETTINGS_DEFAULTS: dict[str, int | str | bool | list[str]] = {
    "context_messages": 5,
    "summarize_threshold": 30,
    "knowledge_max_facts": 50,
    "max_replan_depth": 5,
    "max_validation_retries": 3,
    "max_output_size": 1_048_576,
    "max_message_size": 65536,
    "max_queue_size": 50,
    "max_plan_tasks": 20,
    "exec_timeout": 120,
    "worker_idle_timeout": 300,
    "host": "0.0.0.0",
    "port": 8333,
    "webhook_allow_list": [],
    "webhook_require_https": True,
    "webhook_secret": "",
    "webhook_max_payload": 1048576,
    "bot_name": "Kiso",
    "fast_path_enabled": True,
}

MODEL_DEFAULTS: dict[str, str] = {
    "planner": "minimax/minimax-m2.5",
    "reviewer": "deepseek/deepseek-v3.2",
    "curator": "deepseek/deepseek-v3.2",
    "worker": "deepseek/deepseek-v3.2",
    "summarizer": "deepseek/deepseek-v3.2",
    "paraphraser": "deepseek/deepseek-v3.2",
    "messenger": "deepseek/deepseek-v3.2",
    "searcher": "google/gemini-2.5-flash-lite:online",
}


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
    settings: dict[str, int | str | list[str]]
    raw: dict  # full parsed TOML for future use


class ConfigError(Exception):
    """Raised when config is invalid (for runtime reload)."""


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

    # --- models (optional, with defaults) ---
    models_raw = raw.get("models", {})
    models = {**MODEL_DEFAULTS, **models_raw}

    # --- settings (optional, with defaults) ---
    settings_raw = raw.get("settings", {})
    settings = {**SETTINGS_DEFAULTS, **settings_raw}

    return Config(
        tokens=tokens,
        providers=providers,
        users=users,
        models=models,
        settings=settings,
        raw=raw,
    )


def load_config(path: Path | None = None) -> Config:
    """Load and validate config. Exits on error."""
    return _build_config(path or CONFIG_PATH, _die)


def reload_config(path: Path | None = None) -> Config:
    """Reload config at runtime. Raises ConfigError on failure."""
    def _raise(msg: str) -> None:
        raise ConfigError(msg)
    return _build_config(path or CONFIG_PATH, _raise)
