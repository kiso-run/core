"""Parser for ``[connectors.<name>]`` sections of ``config.toml``.

Kiso supervises external connector processes; it does not install them.
Users declare the connectors they want to run in their ``config.toml``,
one ``[connectors.<name>]`` section per connector, with ``command``,
``args``, ``env``, and optional ``cwd`` / ``token`` / ``webhook``. This
module parses and validates those sections into frozen
``ConnectorConfig`` dataclasses that the supervisor and the connector
CLI consume.

String fields (``command``, ``args`` entries, ``env`` values, ``cwd``,
``token``, ``webhook``) support ``${env:VAR_NAME}`` expansion at parse
time: the substitution reads from the kiso process env, raises
``ConnectorConfigError`` if the referenced variable is missing, and
passes plain dollar signs through unchanged. Expansion happens at
config load, so the supervisor receives fully-resolved values with no
shell semantics.

Per-connector ``env`` dicts must not contain keys starting with
``KISO_``: those keys are reserved for kiso-internal secrets, and
allowing a connector config to set them would let a connector
impersonate or exfiltrate kiso state.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_ENV_REF_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


class ConnectorConfigError(Exception):
    """Raised when a ``[connectors.<name>]`` section is invalid.

    The message always identifies the offending connector name and the
    specific field so the user can fix the config quickly.
    """


@dataclass(frozen=True)
class ConnectorConfig:
    """Parsed and validated ``[connectors.<name>]`` section.

    - ``command`` is required: the executable to spawn (e.g. ``uvx``,
      ``python``, a full path).
    - ``args``, ``env``, ``cwd`` describe how to launch it.
    - ``token`` is an optional per-connector API token used when the
      connector authenticates to kiso's ``/msg`` endpoint.
    - ``webhook`` is an optional URL kiso posts results to; HMAC signing
      uses the globally configured ``config.webhook_secret``.
    - ``enabled`` defaults to True; False entries parse but the
      supervisor refuses to start them.
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    token: str | None = None
    webhook: str | None = None
    enabled: bool = True


def parse_connectors_section(raw: Any) -> dict[str, ConnectorConfig]:
    """Parse the raw ``connectors`` sub-table from a loaded TOML config.

    ``raw`` is the value of ``config["connectors"]`` when the user wrote
    ``[connectors.discord]`` etc., so it looks like::

        {"discord": {"command": "uvx", "args": ["kiso-discord-connector"], ...}}

    Returns a dict keyed by connector name. An empty or ``None`` input
    yields an empty dict. Any malformed entry raises
    ``ConnectorConfigError`` with a message naming the offending
    connector and field.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise ConnectorConfigError("[connectors] must be a table of connector configs")

    connectors: dict[str, ConnectorConfig] = {}
    for name, section in raw.items():
        connectors[name] = _parse_one(name, section)
    return connectors


def _parse_one(name: str, section: Any) -> ConnectorConfig:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConnectorConfigError(
            f"[connectors.{name}]: connector name must match {NAME_RE.pattern}"
        )
    if not isinstance(section, dict):
        raise ConnectorConfigError(f"[connectors.{name}] must be a table")

    command = section.get("command")
    if not isinstance(command, str) or not command:
        raise ConnectorConfigError(
            f"[connectors.{name}]: a non-empty 'command' is required"
        )

    raw_args = section.get("args", []) or []
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        raise ConnectorConfigError(
            f"[connectors.{name}]: 'args' must be a list of strings"
        )

    cwd_raw = section.get("cwd")
    if cwd_raw is not None and not isinstance(cwd_raw, str):
        raise ConnectorConfigError(
            f"[connectors.{name}]: 'cwd' must be a string or absent"
        )

    token_raw = section.get("token")
    if token_raw is not None and not isinstance(token_raw, str):
        raise ConnectorConfigError(
            f"[connectors.{name}]: 'token' must be a string or absent"
        )

    webhook_raw = section.get("webhook")
    if webhook_raw is not None and not isinstance(webhook_raw, str):
        raise ConnectorConfigError(
            f"[connectors.{name}]: 'webhook' must be a string or absent"
        )

    env = _check_env_denylist(name, section.get("env", {}) or {})
    env = _expand_dict(name, "env", env)

    enabled = bool(section.get("enabled", True))

    return ConnectorConfig(
        name=name,
        command=_expand_str(name, "command", command),
        args=[_expand_str(name, f"args[{i}]", a) for i, a in enumerate(raw_args)],
        env=env,
        cwd=_expand_str(name, "cwd", cwd_raw) if cwd_raw else None,
        token=_expand_str(name, "token", token_raw) if token_raw else None,
        webhook=_expand_str(name, "webhook", webhook_raw) if webhook_raw else None,
        enabled=enabled,
    )


def _check_env_denylist(connector_name: str, env: Any) -> dict[str, str]:
    if not isinstance(env, dict):
        raise ConnectorConfigError(
            f"[connectors.{connector_name}]: 'env' must be a table of string:string"
        )
    for key in env.keys():
        if not isinstance(key, str):
            raise ConnectorConfigError(
                f"[connectors.{connector_name}]: 'env' keys must be strings"
            )
        if key.startswith("KISO_"):
            raise ConnectorConfigError(
                f"[connectors.{connector_name}]: 'env' may not set KISO_* variables "
                f"(reserved for kiso-internal secrets); got {key!r}"
            )
    for key, value in env.items():
        if not isinstance(value, str):
            raise ConnectorConfigError(
                f"[connectors.{connector_name}]: 'env.{key}' must be a string"
            )
    return dict(env)


def _expand_str(connector_name: str, field_path: str, value: str) -> str:
    """Replace every ``${env:VAR}`` in *value* with ``os.environ[VAR]``.

    Unknown variables raise ``ConnectorConfigError``. Plain dollar signs
    that are not followed by the exact ``{env:...}`` form are left
    untouched.
    """

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        try:
            return os.environ[var]
        except KeyError:
            raise ConnectorConfigError(
                f"[connectors.{connector_name}]: {field_path} references "
                f"${{env:{var}}} but {var} is not set in the environment"
            ) from None

    return _ENV_REF_RE.sub(_sub, value)


def _expand_dict(
    connector_name: str, field_path: str, d: dict[str, str]
) -> dict[str, str]:
    return {
        k: _expand_str(connector_name, f"{field_path}.{k}", v) for k, v in d.items()
    }
