"""URL resolver for ``kiso mcp install --from-url``.

Given a user-supplied URL or pseudo-URL identifying an MCP server,
produces a normalised install plan: a dict describing which
``command`` and ``args`` the server should be invoked with, plus
any pre-install steps (git clone, uv venv, uv pip install, etc.).

Policy: **never** run ``pip install`` with the system Python and
**never** run ``npm install -g``. Ephemeral runners only:

- npm-packaged servers → ``command="npx"``, ``args=["-y", <pkg>]``.
  No install step; npx caches on first use.
- PyPI-packaged servers → ``command="uvx"``, ``args=[<pkg>]``.
  No install step; uv caches on first use.
- Git-clone servers → clone into ``~/.kiso/mcp/servers/<name>/``,
  create an isolated venv via ``uv venv``, install via
  ``uv pip install -e .``, point ``command`` at the venv entry
  point declared in the repo's ``pyproject.toml[project.scripts]``
  or ``package.json.bin``.
- Raw ``server.json`` or ``mcp.json`` URL → fetch, parse, return
  the normalised config directly.

The resolver is deliberately offline-friendly where it can be —
the unit tests drive it with mocked HTTP responses and a local
git fixture so the core logic is fully testable without network.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from kiso.config import KISO_DIR
from kiso.mcp.config import NAME_RE


MCP_SERVERS_DIR = KISO_DIR / "mcp" / "servers"


@dataclass
class ResolvedServer:
    """Normalised install plan for a single MCP server.

    Consumers (the CLI) render this into a TOML section and optionally
    run the pre-install steps before writing the config.
    """

    name: str
    transport: str  # "stdio" or "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    pre_install: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Optional thunk invoked by the CLI after `pre_install` runs to
    # finalize `command`. Used by the github resolver to read the
    # cloned repo's `pyproject.toml [project.scripts]` and pick the
    # entry-point binary, since that information is not available
    # until the editable install has populated the venv.
    post_install_command_resolver: Callable[[], str] | None = field(
        default=None, repr=False, compare=False
    )


class InstallResolverError(Exception):
    """Raised when a URL cannot be resolved into a concrete install plan."""


_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
_SCOPED_NPM_RE = re.compile(r"^@[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
_NAME_FROM_PKG_RE = re.compile(r"[^a-z0-9_-]+")


def _sanitize_name(raw: str, fallback: str = "server") -> str:
    """Produce a config-safe name from a package or repo identifier.

    The result matches ``NAME_RE`` in ``kiso.mcp.config``: lowercase
    letters/digits/underscore/dash, first char alpha/underscore,
    max 32 chars. If sanitisation yields an empty string, returns
    *fallback*.
    """
    lower = raw.lower()
    cleaned = _NAME_FROM_PKG_RE.sub("-", lower).strip("-")
    if not cleaned:
        return fallback
    if not cleaned[0].isalpha() and cleaned[0] != "_":
        cleaned = "m" + cleaned
    return cleaned[:32]


def check_runtime_dependencies() -> list[str]:
    """Return the list of runtime binaries missing from PATH.

    ``uv`` and ``npx`` are the two ephemeral runners Kiso supports
    for MCP server execution. Missing either of them blocks
    installing servers of the corresponding kind; the CLI surfaces
    the list to the user with install pointers.
    """
    missing: list[str] = []
    if shutil.which("uv") is None:
        missing.append("uv")
    if shutil.which("npx") is None:
        missing.append("npx")
    return missing


def resolve_from_url(
    url: str,
    *,
    name_hint: str | None = None,
    http_fetcher: Callable[[str], dict] | None = None,
) -> ResolvedServer:
    """Parse *url* and return a :class:`ResolvedServer`.

    Supported URL forms:

    - ``https://www.pulsemcp.com/servers/<name>`` → fetch
      ``/servers/<name>/serverjson`` and normalise
    - ``https://www.npmjs.com/package/@scope/name`` or
      ``https://www.npmjs.com/package/name`` → npx config
    - ``npm:@scope/name`` or ``npm:name`` → npx config
    - ``https://pypi.org/project/name/`` or
      ``https://pypi.org/project/name`` → uvx config
    - ``pypi:name`` → uvx config
    - ``https://github.com/<owner>/<repo>`` → git-clone install plan
    - Any URL whose path ends with ``server.json`` or ``mcp.json``
      → fetch JSON and normalise

    Raises :class:`InstallResolverError` for unrecognised forms.
    """
    if not url or not isinstance(url, str):
        raise InstallResolverError("empty or non-string URL")

    stripped = url.strip()

    if stripped.startswith("npm:"):
        return _resolve_npm_pkg(stripped[len("npm:"):], name_hint)
    if stripped.startswith("pypi:"):
        return _resolve_pypi_pkg(stripped[len("pypi:"):], name_hint)

    parsed = urlparse(stripped)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if host.endswith("pulsemcp.com"):
        return _resolve_pulsemcp(stripped, name_hint, http_fetcher)
    if host.endswith("npmjs.com"):
        if "/package/" in path:
            pkg = path.split("/package/", 1)[1].rstrip("/")
            return _resolve_npm_pkg(pkg, name_hint)
    if host.endswith("pypi.org"):
        if "/project/" in path:
            pkg = path.split("/project/", 1)[1].strip("/")
            return _resolve_pypi_pkg(pkg, name_hint)
    if host.endswith("github.com"):
        return _resolve_github(stripped, name_hint)
    if path.endswith("server.json") or path.endswith("mcp.json"):
        return _resolve_raw_manifest(stripped, name_hint, http_fetcher)

    raise InstallResolverError(
        f"unrecognised MCP install URL: {stripped!r}. "
        f"Supported: pulsemcp.com, github.com, npmjs.com, pypi.org, "
        f"npm:<pkg>, pypi:<pkg>, raw server.json URL"
    )


# ---------------------------------------------------------------------------
# Per-form resolvers
# ---------------------------------------------------------------------------


def _resolve_npm_pkg(pkg: str, name_hint: str | None) -> ResolvedServer:
    pkg = pkg.strip().strip("/")
    if not pkg:
        raise InstallResolverError("empty npm package name")
    if not (_SCOPED_NPM_RE.match(pkg) or _PACKAGE_NAME_RE.match(pkg)):
        raise InstallResolverError(f"invalid npm package name: {pkg!r}")
    name = name_hint or _sanitize_name(pkg.split("/")[-1])
    args = ["-y", pkg]
    notes = [
        f"Installs ephemerally via npx on first use. No global install.",
        f"Package: {pkg}",
    ]
    # @playwright/mcp defaults to launching the `chrome` channel (system
    # Chrome at /opt/google/chrome/chrome). On hosts without system
    # Chrome (the common case) this fails. Force the bundled Chromium
    # browser so the install works out of the box after a one-time
    # `npx playwright install chromium`.
    if pkg == "@playwright/mcp":
        args.append("--browser=chromium")
        notes.append(
            "Auto-added --browser=chromium so the bundled Playwright "
            "Chromium is used (no system Chrome required)."
        )
    return ResolvedServer(
        name=name,
        transport="stdio",
        command="npx",
        args=args,
        notes=notes,
    )


def _resolve_pypi_pkg(pkg: str, name_hint: str | None) -> ResolvedServer:
    pkg = pkg.strip().strip("/")
    if not pkg:
        raise InstallResolverError("empty PyPI package name")
    if not _PACKAGE_NAME_RE.match(pkg):
        raise InstallResolverError(f"invalid PyPI package name: {pkg!r}")
    name = name_hint or _sanitize_name(pkg)
    return ResolvedServer(
        name=name,
        transport="stdio",
        command="uvx",
        args=[pkg],
        notes=[
            f"Installs ephemerally via uvx on first use. No global install.",
            f"Package: {pkg}",
        ],
    )


def _resolve_pulsemcp(
    url: str, name_hint: str | None, http_fetcher: Callable[[str], dict] | None
) -> ResolvedServer:
    # Pulsemcp exposes a standardized server.json endpoint at
    # /servers/<slug>/serverjson. We transform the UI URL to the
    # JSON endpoint and fetch it.
    if "/serverjson" not in url:
        json_url = url.rstrip("/") + "/serverjson"
    else:
        json_url = url
    payload = _fetch_json(json_url, http_fetcher)
    return _resolve_from_manifest(payload, name_hint, source=url)


def _resolve_raw_manifest(
    url: str, name_hint: str | None, http_fetcher: Callable[[str], dict] | None
) -> ResolvedServer:
    payload = _fetch_json(url, http_fetcher)
    return _resolve_from_manifest(payload, name_hint, source=url)


def _select_entry_point_from_scripts(scripts: dict[str, str]) -> str:
    """Deterministically pick a venv binary name from `[project.scripts]`.

    Selection order:
    1. Zero entries → ``InstallResolverError`` (the upstream repo
       declares no script, kiso can't run it as an MCP).
    2. One entry → that one, regardless of name.
    3. Multiple entries → prefer scripts matching ``kiso-*-mcp``, then
       ``*-mcp``, then alphabetical first. Ties are broken
       alphabetically so the choice is reproducible across hosts.
    """
    if not scripts:
        raise InstallResolverError(
            "no [project.scripts] entry in pyproject.toml — cannot "
            "determine MCP entry-point binary"
        )
    names = sorted(scripts.keys())
    if len(names) == 1:
        return names[0]
    kiso_mcp = [n for n in names if n.startswith("kiso-") and n.endswith("-mcp")]
    if kiso_mcp:
        return kiso_mcp[0]
    dash_mcp = [n for n in names if n.endswith("-mcp")]
    if dash_mcp:
        return dash_mcp[0]
    return names[0]


def _read_pyproject_scripts(clone_dir: Path) -> dict[str, str]:
    pyproject = clone_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise InstallResolverError(
            f"pyproject.toml not found in {clone_dir} — cannot "
            "determine MCP entry-point binary"
        )
    data = tomllib.loads(pyproject.read_text())
    project = data.get("project") or {}
    return dict(project.get("scripts") or {})


def _resolve_github(url: str, name_hint: str | None) -> ResolvedServer:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise InstallResolverError(
            f"github URL missing owner/repo: {url!r}"
        )
    owner, repo = parts[0], parts[1].removesuffix(".git")
    name = name_hint or _sanitize_name(repo, fallback="server")
    clone_dir = MCP_SERVERS_DIR / name
    # Pre-install steps: clone, then create venv, then editable install.
    pre_install = [
        ["git", "clone", "--depth", "1", url, str(clone_dir)],
        ["uv", "venv", str(clone_dir / ".venv")],
        ["uv", "pip", "install", "--python", str(clone_dir / ".venv" / "bin" / "python"), "-e", str(clone_dir)],
    ]

    def _resolve_command_after_install() -> str:
        scripts = _read_pyproject_scripts(clone_dir)
        entry = _select_entry_point_from_scripts(scripts)
        return str(clone_dir / ".venv" / "bin" / entry)

    return ResolvedServer(
        name=name,
        transport="stdio",
        # Placeholder — replaced by the CLI after pre_install runs.
        command=str(clone_dir / ".venv" / "bin" / name),
        args=[],
        cwd=str(clone_dir),
        pre_install=pre_install,
        post_install_command_resolver=_resolve_command_after_install,
        notes=[
            f"Git-clone install from {owner}/{repo}",
            f"Clone target: {clone_dir}",
            "Entry-point auto-detected from pyproject.toml [project.scripts] after install.",
        ],
    )


def _resolve_from_manifest(
    payload: dict, name_hint: str | None, *, source: str
) -> ResolvedServer:
    """Normalise a ``server.json`` / ``mcp.json`` / pulsemcp payload.

    Accepts a handful of loose shapes (pulsemcp's schema, Claude
    Desktop's ``mcpServers`` block, a bare ``{command, args, env}``
    object) and turns them into a ``ResolvedServer``.
    """
    if not isinstance(payload, dict):
        raise InstallResolverError(f"{source}: expected JSON object")

    # Claude Desktop / Claude Code shape: {"mcpServers": {name: {...}}}
    servers = payload.get("mcpServers")
    if isinstance(servers, dict) and servers:
        name, entry = next(iter(servers.items()))
        if not isinstance(entry, dict):
            raise InstallResolverError(f"{source}: mcpServers entry is not an object")
        return _build_resolved_from_entry(name, entry, name_hint, source)

    # Pulsemcp / bare shape
    name = name_hint or payload.get("name") or "server"
    return _build_resolved_from_entry(name, payload, name_hint, source)


def _build_resolved_from_entry(
    raw_name: str,
    entry: dict,
    name_hint: str | None,
    source: str,
) -> ResolvedServer:
    name = _sanitize_name(name_hint or raw_name)

    # HTTP form
    url = entry.get("url")
    if isinstance(url, str) and url:
        headers = entry.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise InstallResolverError(f"{source}: headers must be a table")
        return ResolvedServer(
            name=name,
            transport="http",
            url=url,
            headers={k: str(v) for k, v in (headers or {}).items()},
            notes=[f"HTTP MCP server — source: {source}"],
        )

    # stdio form
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise InstallResolverError(
            f"{source}: entry missing 'command' (required for stdio) or 'url' (required for http)"
        )
    args = entry.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise InstallResolverError(f"{source}: 'args' must be a list of strings")
    env = entry.get("env") or {}
    if not isinstance(env, dict):
        raise InstallResolverError(f"{source}: 'env' must be a table")
    for key in env.keys():
        if str(key).startswith("KISO_"):
            raise InstallResolverError(
                f"{source}: env may not set KISO_* variables"
            )
    env_str = {str(k): str(v) for k, v in env.items()}
    return ResolvedServer(
        name=name,
        transport="stdio",
        command=command,
        args=list(args),
        env=env_str,
        notes=[f"Stdio MCP server — source: {source}"],
    )


def _fetch_json(url: str, http_fetcher: Callable[[str], dict] | None) -> dict:
    if http_fetcher is not None:
        return http_fetcher(url)
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        raise InstallResolverError(f"failed to fetch {url}: {e}") from e
    except ValueError as e:
        raise InstallResolverError(f"{url}: response was not valid JSON: {e}") from e
