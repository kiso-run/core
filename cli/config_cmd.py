"""Config management CLI commands."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import tomli_w

from cli._admin import require_admin
from cli.render import die
from kiso.config import CONFIG_PATH, SETTINGS_DEFAULTS

# Module-level path used by command functions; can be patched in tests.
CONFIG_PATH_DEFAULT: Path = CONFIG_PATH


def run_config_command(args) -> None:
    """Dispatch to the appropriate config subcommand."""
    from cli._admin import dispatch_subcommand
    dispatch_subcommand(args, "config_cmd", {
        "set": config_set, "get": config_get, "list": config_list,
    }, "usage: kiso config {set,get,list}")


def _read_raw(path: Path | None = None) -> dict:
    """Read and parse config.toml."""
    p = path or CONFIG_PATH_DEFAULT
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _write_raw(raw: dict, path: Path | None = None) -> None:
    """Write raw dict back to config.toml."""
    p = path or CONFIG_PATH_DEFAULT
    with open(p, "wb") as f:
        tomli_w.dump(raw, f)


def _coerce_value(key: str, raw_value: str):
    """Coerce a string value to match the type of the default for *key*."""
    default = SETTINGS_DEFAULTS[key]
    target_type = type(default)

    if target_type is bool:
        lowered = raw_value.lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        die(f"cannot convert '{raw_value}' to bool (use true/false)")
    if target_type is int:
        try:
            return int(raw_value)
        except ValueError:
            die(f"cannot convert '{raw_value}' to int")
    if target_type is float:
        try:
            return float(raw_value)
        except ValueError:
            die(f"cannot convert '{raw_value}' to float")
    if target_type is list:
        # Comma-separated list
        if raw_value == "":
            return []
        return [s.strip() for s in raw_value.split(",") if s.strip()]
    # str
    return raw_value


def config_set(args) -> None:
    """Set a config setting in config.toml and hot-reload."""
    require_admin()

    key = args.key
    raw_value = args.value

    if key not in SETTINGS_DEFAULTS:
        die(f"unknown setting '{key}' — run 'kiso config list' to see valid keys")

    value = _coerce_value(key, raw_value)

    raw = _read_raw()
    if "settings" not in raw:
        raw["settings"] = {}
    raw["settings"][key] = value
    _write_raw(raw)

    _call_reload(args)
    print(f"{key} = {value}")


def config_get(args) -> None:
    """Get a config setting value."""
    key = args.key

    if key not in SETTINGS_DEFAULTS:
        die(f"unknown setting '{key}' — run 'kiso config list' to see valid keys")

    raw = _read_raw()
    settings = raw.get("settings", {})
    value = settings.get(key, SETTINGS_DEFAULTS[key])
    print(f"{key} = {value}")


def config_list(args) -> None:
    """List all config settings with current values."""
    raw = _read_raw()
    settings = raw.get("settings", {})

    for key in sorted(SETTINGS_DEFAULTS):
        value = settings.get(key, SETTINGS_DEFAULTS[key])
        marker = "" if key not in settings else " *"
        print(f"  {key} = {value}{marker}")


def _call_reload(args) -> None:
    """Call POST /admin/reload-config to hot-reload the running server."""
    import getpass

    from cli._http import cli_post

    user = getattr(args, "user", None) or getpass.getuser()
    cli_post(args, "/admin/reload-config", params={"user": user})
