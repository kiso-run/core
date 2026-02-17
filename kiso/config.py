"""Load and validate ~/.kiso/config.toml."""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KISO_DIR = Path.home() / ".kiso"
CONFIG_PATH = KISO_DIR / "config.toml"

NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

SETTINGS_DEFAULTS: dict[str, int | str | list[str]] = {
    "context_messages": 5,
    "summarize_threshold": 30,
    "knowledge_max_facts": 50,
    "max_replan_depth": 3,
    "max_validation_retries": 3,
    "exec_timeout": 120,
    "worker_idle_timeout": 300,
    "host": "0.0.0.0",
    "port": 8333,
    "webhook_allow_list": [],
    "webhook_require_https": True,
    "webhook_secret": "",
    "webhook_max_payload": 1048576,
}

MODEL_DEFAULTS: dict[str, str] = {
    "planner": "moonshotai/kimi-k2.5",
    "reviewer": "moonshotai/kimi-k2.5",
    "curator": "moonshotai/kimi-k2.5",
    "worker": "deepseek/deepseek-v3.2",
    "summarizer": "deepseek/deepseek-v3.2",
}


@dataclass(frozen=True)
class Provider:
    base_url: str
    api_key_env: str | None = None


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


def _die(msg: str) -> None:
    print(f"config error: {msg}", file=sys.stderr)
    sys.exit(1)


def _check_name(name: str, kind: str) -> None:
    if not NAME_RE.match(name):
        _die(f"{kind} '{name}' does not match {NAME_RE.pattern}")


def load_config(path: Path | None = None) -> Config:
    """Load and validate config. Exits on error."""
    path = path or CONFIG_PATH
    if not path.exists():
        _die(f"{path} not found")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    # --- required sections ---
    for section in ("tokens", "providers", "users"):
        if section not in raw or not raw[section]:
            _die(f"[{section}] section is missing or empty")

    # --- tokens ---
    tokens: dict[str, str] = {}
    for name, value in raw["tokens"].items():
        _check_name(name, "token name")
        if not isinstance(value, str) or not value:
            _die(f"token '{name}' must be a non-empty string")
        tokens[name] = value

    # --- providers ---
    providers: dict[str, Provider] = {}
    for name, prov in raw["providers"].items():
        _check_name(name, "provider name")
        if not isinstance(prov, dict):
            _die(f"provider '{name}' must be a table")
        if "base_url" not in prov:
            _die(f"provider '{name}' is missing base_url")
        providers[name] = Provider(
            base_url=prov["base_url"],
            api_key_env=prov.get("api_key_env"),
        )

    # --- users ---
    users: dict[str, User] = {}
    all_aliases: dict[str, str] = {}  # "connector:platform_id" → username

    for uname, udata in raw["users"].items():
        _check_name(uname, "username")
        if not isinstance(udata, dict):
            _die(f"user '{uname}' must be a table")

        role = udata.get("role")
        if role not in ("admin", "user"):
            _die(f"user '{uname}': role must be 'admin' or 'user', got '{role}'")

        # skills
        skills = udata.get("skills")
        if role == "user":
            if skills is None:
                _die(f"user '{uname}' has role=user but no 'skills' field")
            if skills != "*" and not isinstance(skills, list):
                _die(f"user '{uname}': skills must be '*' or a list of skill names")

        # aliases
        aliases_raw = udata.get("aliases", {})
        if not isinstance(aliases_raw, dict):
            _die(f"user '{uname}': aliases must be a table")
        aliases: dict[str, str] = {}
        for connector, platform_id in aliases_raw.items():
            key = f"{connector}:{platform_id}"
            if key in all_aliases:
                _die(
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
