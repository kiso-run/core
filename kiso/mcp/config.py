"""Parser for ``[mcp.<name>]`` sections of ``config.toml``.

Kiso is a consumer-only MCP client: users declare the MCP servers they
want to use in their config.toml, one ``[mcp.<name>]`` section per
server, with ``transport = "stdio"`` or ``transport = "http"``. This
module parses and validates those sections into frozen ``MCPServer``
dataclasses that the rest of the MCP client uses.

String fields in the config (``command``, ``args`` entries, ``env``
values, ``cwd``, ``url``, ``headers`` values, ``auth`` values) support
``${env:VAR_NAME}`` expansion at parse time — the substitution reads
from the kiso process env, raises ``MCPConfigError`` if the referenced
variable is missing, and passes plain dollar signs through unchanged.
Expansion happens at config load, never at subprocess spawn time, so
the subprocess receives fully-resolved values with no shell semantics.

Per-server ``env`` dicts must not contain keys starting with ``KISO_``:
those keys are reserved for kiso-internal secrets, and allowing a
server config to set them would let an MCP server impersonate or
exfiltrate kiso state. The deny-list is enforced by name, before any
expansion, so obfuscation via ``${env:...}`` in the value cannot
bypass it.
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_TRANSPORTS = ("stdio", "http")
_ENV_REF_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_SESSION_REF_RE = re.compile(r"\$\{session:([A-Za-z_][A-Za-z0-9_]*)\}")
_SESSION_TOKEN_KINDS = ("workspace", "id")
_SANDBOX_MODES = ("role_based", "never")


class MCPConfigError(Exception):
    """Raised when a ``[mcp.<name>]`` section is invalid or cannot be
    parsed. The message always identifies the offending server name and
    the specific field so the user can fix the config quickly."""


@dataclass(frozen=True)
class MCPServer:
    """Parsed and validated ``[mcp.<name>]`` section.

    Shape depends on ``transport``:

    - ``stdio``: ``command`` is required; ``args``, ``env``, ``cwd``
      are optional. The client spawns this command as a subprocess.
    - ``http``: ``url`` is required; ``headers``, ``auth`` are optional.
      The client issues HTTP requests to this URL.

    Common fields:

    - ``enabled`` defaults to True
    - ``timeout_s`` defaults to 60.0 seconds
    """

    name: str
    transport: str
    # stdio fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # http fields
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    auth: dict[str, Any] | None = None
    # common
    enabled: bool = True
    timeout_s: float = 60.0
    sandbox: str = "role_based"

    @property
    def is_session_scoped(self) -> bool:
        """True if any string field still contains a ``${session:*}`` token."""
        return any(
            _SESSION_REF_RE.search(s) is not None
            for s in _iter_string_fields(self)
        )


def _iter_string_fields(server: MCPServer):
    if server.command:
        yield server.command
    yield from server.args
    yield from server.env.values()
    if server.cwd:
        yield server.cwd
    if server.url:
        yield server.url
    yield from server.headers.values()
    if server.auth:
        for v in server.auth.values():
            if isinstance(v, str):
                yield v


def resolve_session_tokens(
    server: MCPServer, session_id: str, workspace: Path | str
) -> MCPServer:
    """Substitute ``${session:workspace}`` / ``${session:id}`` per session.

    Returns the original object unchanged when the server has no
    session tokens, so the manager can rely on identity to detect
    shared (global-scope) clients.
    """
    if not server.is_session_scoped:
        return server

    mapping = {"workspace": str(workspace), "id": session_id}

    def sub(value: str, field_path: str) -> str:
        def _sub(match: re.Match[str]) -> str:
            kind = match.group(1)
            if kind not in _SESSION_TOKEN_KINDS:
                raise MCPConfigError(
                    f"[mcp.{server.name}]: {field_path} references "
                    f"${{session:{kind}}} but only "
                    f"${{session:workspace}} and ${{session:id}} are supported"
                )
            return mapping[kind]

        return _SESSION_REF_RE.sub(_sub, value)

    def sub_dict(d: dict[str, str], prefix: str) -> dict[str, str]:
        return {k: sub(v, f"{prefix}.{k}") for k, v in d.items()}

    def sub_auth(auth: dict[str, Any] | None) -> dict[str, Any] | None:
        if auth is None:
            return None
        out: dict[str, Any] = {}
        for k, v in auth.items():
            out[k] = sub(v, f"auth.{k}") if isinstance(v, str) else v
        return out

    return dataclasses.replace(
        server,
        command=sub(server.command, "command") if server.command else None,
        args=[sub(a, f"args[{i}]") for i, a in enumerate(server.args)],
        env=sub_dict(server.env, "env"),
        cwd=sub(server.cwd, "cwd") if server.cwd else None,
        url=sub(server.url, "url") if server.url else None,
        headers=sub_dict(server.headers, "headers"),
        auth=sub_auth(server.auth),
    )


def parse_mcp_section(raw: dict | None) -> dict[str, MCPServer]:
    """Parse the raw ``mcp`` sub-table from a loaded TOML config.

    ``raw`` is the value of ``config["mcp"]`` when the user wrote
    ``[mcp.github]`` etc., so it looks like::

        {"github": {"transport": "stdio", "command": "npx", ...},
         "maps":   {"transport": "http",  "url": "...", ...}}

    Returns a dict keyed by server name. An empty or ``None`` input
    yields an empty dict. Any malformed entry raises ``MCPConfigError``
    with a message naming the offending server and field.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise MCPConfigError("[mcp] must be a table of server configs")

    servers: dict[str, MCPServer] = {}
    for name, section in raw.items():
        servers[name] = _parse_one(name, section)
    return servers


def _parse_one(name: str, section: Any) -> MCPServer:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise MCPConfigError(
            f"[mcp.{name}]: server name must match {NAME_RE.pattern}"
        )
    if not isinstance(section, dict):
        raise MCPConfigError(f"[mcp.{name}] must be a table")

    transport = section.get("transport")
    if transport not in _TRANSPORTS:
        raise MCPConfigError(
            f"[mcp.{name}]: transport must be one of {_TRANSPORTS}, "
            f"got {transport!r}"
        )

    env = _check_env_denylist(name, section.get("env", {}) or {})
    env = _expand_dict(name, "env", env)

    timeout_s = float(section.get("timeout_s", 60.0))
    if timeout_s <= 0:
        raise MCPConfigError(
            f"[mcp.{name}]: timeout_s must be positive, got {timeout_s}"
        )
    enabled = bool(section.get("enabled", True))

    sandbox = section.get("sandbox", "role_based")
    if sandbox not in _SANDBOX_MODES:
        raise MCPConfigError(
            f"[mcp.{name}]: sandbox must be one of {_SANDBOX_MODES}, "
            f"got {sandbox!r}"
        )

    if transport == "stdio":
        command = section.get("command")
        if not isinstance(command, str) or not command:
            raise MCPConfigError(
                f"[mcp.{name}]: stdio transport requires a non-empty 'command'"
            )
        raw_args = section.get("args", []) or []
        if not isinstance(raw_args, list) or not all(
            isinstance(a, str) for a in raw_args
        ):
            raise MCPConfigError(
                f"[mcp.{name}]: 'args' must be a list of strings"
            )
        cwd_raw = section.get("cwd")
        if cwd_raw is not None and not isinstance(cwd_raw, str):
            raise MCPConfigError(f"[mcp.{name}]: 'cwd' must be a string or absent")

        return MCPServer(
            name=name,
            transport="stdio",
            command=_expand_str(name, "command", command),
            args=[_expand_str(name, f"args[{i}]", a) for i, a in enumerate(raw_args)],
            env=env,
            cwd=_expand_str(name, "cwd", cwd_raw) if cwd_raw else None,
            enabled=enabled,
            timeout_s=timeout_s,
            sandbox=sandbox,
        )

    # transport == "http"
    url = section.get("url")
    if not isinstance(url, str) or not url:
        raise MCPConfigError(
            f"[mcp.{name}]: http transport requires a non-empty 'url'"
        )
    raw_headers = section.get("headers", {}) or {}
    if not isinstance(raw_headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_headers.items()
    ):
        raise MCPConfigError(
            f"[mcp.{name}]: 'headers' must be a table of string:string"
        )
    auth = section.get("auth")
    if auth is not None and not isinstance(auth, dict):
        raise MCPConfigError(f"[mcp.{name}]: 'auth' must be a table or absent")

    return MCPServer(
        name=name,
        transport="http",
        url=_expand_str(name, "url", url),
        headers=_expand_dict(name, "headers", raw_headers),
        auth=_expand_auth(name, auth) if auth else None,
        enabled=enabled,
        timeout_s=timeout_s,
        sandbox=sandbox,
    )


def _check_env_denylist(server_name: str, env: Any) -> dict[str, str]:
    if not isinstance(env, dict):
        raise MCPConfigError(
            f"[mcp.{server_name}]: 'env' must be a table of string:string"
        )
    for key in env.keys():
        if not isinstance(key, str):
            raise MCPConfigError(
                f"[mcp.{server_name}]: 'env' keys must be strings"
            )
        if key.startswith("KISO_"):
            raise MCPConfigError(
                f"[mcp.{server_name}]: 'env' may not set KISO_* variables "
                f"(reserved for kiso-internal secrets); got {key!r}"
            )
    for key, value in env.items():
        if not isinstance(value, str):
            raise MCPConfigError(
                f"[mcp.{server_name}]: 'env.{key}' must be a string"
            )
    return dict(env)


def _expand_str(server_name: str, field_path: str, value: str) -> str:
    """Replace every ``${env:VAR}`` in *value* with ``os.environ[VAR]``.

    Unknown variables raise ``MCPConfigError``. Plain dollar signs that
    are not followed by the exact ``{env:...}`` form are left untouched.
    """

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        try:
            return os.environ[var]
        except KeyError:
            raise MCPConfigError(
                f"[mcp.{server_name}]: {field_path} references "
                f"${{env:{var}}} but {var} is not set in the environment"
            ) from None

    return _ENV_REF_RE.sub(_sub, value)


def _expand_dict(
    server_name: str, field_path: str, d: dict[str, str]
) -> dict[str, str]:
    return {
        k: _expand_str(server_name, f"{field_path}.{k}", v) for k, v in d.items()
    }


def _expand_auth(server_name: str, auth: dict) -> dict:
    out: dict = {}
    for k, v in auth.items():
        if isinstance(v, str):
            out[k] = _expand_str(server_name, f"auth.{k}", v)
        else:
            out[k] = v
    return out
