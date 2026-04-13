"""``kiso mcp`` subcommand implementations.

Manages ``[mcp.<name>]`` sections in ``config.toml``, per-server
credential env files at ``~/.kiso/mcp/<name>.env``, and MCP server
installation from external URLs (pulsemcp, github, npm, pypi,
server.json).

Design notes:

- Kiso is a consumer of the MCP ecosystem: we do not maintain a
  registry of MCP servers and do not bless specific implementations.
  Users bring their own URLs or package identifiers.
- Credentials are never read from chat/stdin prompts by default —
  ``kiso mcp env <name> set KEY VAL`` is the only documented path,
  and it writes to a file with ``0600`` perms. The CLI's ``show``
  sub-subcommand is explicitly opt-in.
- Install is ephemeral where possible (uvx / npx). No global
  ``pip install`` or ``npm install -g``. Git-clone fallback uses
  ``uv venv`` + ``uv pip install -e .`` in an isolated per-server
  venv under ``~/.kiso/mcp/servers/<name>/``.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import tomli_w

from cli.render import die
from kiso.config import KISO_DIR, CONFIG_PATH
from kiso.mcp.config import NAME_RE, MCPConfigError, parse_mcp_section
from kiso.mcp.install import (
    InstallResolverError,
    ResolvedServer,
    check_runtime_dependencies,
    resolve_from_url,
)

MCP_ENV_DIR = KISO_DIR / "mcp"
MCP_LOG_DIR = KISO_DIR / "mcp"


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def add_subcommands(parent: argparse.ArgumentParser) -> None:
    s = parent.add_subparsers(dest="mcp_command")

    s.add_parser("list", help="list configured MCP servers")

    add = s.add_parser("add", help="add an MCP server (direct form)")
    add.add_argument("name", help="server name (matches NAME_RE)")
    add.add_argument("transport", choices=("stdio", "http"))
    add.add_argument("--command", help="stdio: command to spawn")
    add.add_argument("--args", nargs="*", default=[], help="stdio: command args")
    add.add_argument("--cwd", help="stdio: working directory")
    add.add_argument("--env", nargs="*", default=[], help="stdio: KEY=VAL pairs")
    add.add_argument("--url", help="http: server endpoint URL")
    add.add_argument(
        "--header", nargs="*", default=[], help="http: KEY=VAL header pairs"
    )
    add.add_argument(
        "--timeout-s", type=float, default=60.0, help="per-call timeout"
    )

    install = s.add_parser(
        "install", help="install an MCP server from a URL"
    )
    install.add_argument("--from-url", required=True, help="pulsemcp / github / npm: / pypi: / server.json URL")
    install.add_argument("--name", default=None, help="override server config name")
    install.add_argument("--dry-run", action="store_true", help="print the install plan without executing")

    rm = s.add_parser("remove", help="remove an MCP server from config")
    rm.add_argument("name", help="server name")
    rm.add_argument("--yes", action="store_true", help="skip confirmation")

    test = s.add_parser("test", help="initialize + list_methods + shutdown")
    test.add_argument("name", help="server name")

    logs = s.add_parser("logs", help="tail the server stderr log")
    logs.add_argument("name", help="server name")
    logs.add_argument("--tail", type=int, default=50, help="number of lines")

    env = s.add_parser("env", help="manage per-server credential env vars")
    e = env.add_subparsers(dest="mcp_env_command")
    es = e.add_parser("set", help="set a KEY=VAL credential")
    es.add_argument("name", help="server name")
    es.add_argument("key", help="env var name")
    es.add_argument("value", help="env var value")
    eu = e.add_parser("unset", help="remove a KEY credential")
    eu.add_argument("name", help="server name")
    eu.add_argument("key", help="env var name")
    el = e.add_parser("list", help="list keys (values hidden)")
    el.add_argument("name", help="server name")
    eshow = e.add_parser("show", help="print keys AND values (secrets visible)")
    eshow.add_argument("name", help="server name")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def handle(args: argparse.Namespace) -> int:
    cmd = getattr(args, "mcp_command", None)
    if cmd is None:
        print("usage: kiso mcp {list|add|install|remove|test|logs|env}")
        return 2
    if cmd == "list":
        return _cmd_list()
    if cmd == "add":
        return _cmd_add(args)
    if cmd == "install":
        return _cmd_install(args)
    if cmd == "remove":
        return _cmd_remove(args)
    if cmd == "test":
        return _cmd_test(args)
    if cmd == "logs":
        return _cmd_logs(args)
    if cmd == "env":
        return _cmd_env(args)
    die(f"unknown mcp subcommand: {cmd}")
    return 2  # unreachable


# ---------------------------------------------------------------------------
# Config read/write
# ---------------------------------------------------------------------------


def _read_config_raw(config_path: Path | None = None) -> tuple[Path, dict]:
    path = config_path or CONFIG_PATH
    if not path.exists():
        die(f"config file not found: {path}")
    import tomllib
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return path, raw


def _write_config_raw(path: Path, raw: dict) -> None:
    # tomli_w preserves nested tables well and is already a dep.
    path.write_bytes(tomli_w.dumps(raw).encode("utf-8"))


def _get_mcp_sections(raw: dict) -> dict:
    mcp = raw.get("mcp")
    if not isinstance(mcp, dict):
        return {}
    return mcp


def _set_mcp_sections(raw: dict, mcp: dict) -> None:
    if mcp:
        raw["mcp"] = mcp
    else:
        raw.pop("mcp", None)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list() -> int:
    path, raw = _read_config_raw()
    mcp = _get_mcp_sections(raw)
    if not mcp:
        print("(no MCP servers configured)")
        print(f"Config: {path}")
        return 0
    print(f"{'NAME':<20} {'TRANSPORT':<10} {'STATUS':<10} DETAIL")
    for name in sorted(mcp.keys()):
        entry = mcp[name]
        transport = entry.get("transport", "?")
        enabled = entry.get("enabled", True)
        status = "enabled" if enabled else "disabled"
        if transport == "stdio":
            detail = entry.get("command", "?")
        else:
            detail = entry.get("url", "?")
        print(f"{name:<20} {transport:<10} {status:<10} {detail}")
    return 0


# ---------------------------------------------------------------------------
# add (direct form)
# ---------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    name = args.name
    if not NAME_RE.match(name):
        die(f"invalid server name: {name!r} (must match {NAME_RE.pattern})")

    entry: dict = {"transport": args.transport, "timeout_s": args.timeout_s}

    if args.transport == "stdio":
        if not args.command:
            die("stdio transport requires --command")
        entry["command"] = args.command
        if args.args:
            entry["args"] = list(args.args)
        if args.cwd:
            entry["cwd"] = args.cwd
        env = _parse_kv_pairs(args.env, "--env")
        if env:
            entry["env"] = env
    else:
        if not args.url:
            die("http transport requires --url")
        entry["url"] = args.url
        headers = _parse_kv_pairs(args.header, "--header")
        if headers:
            entry["headers"] = headers

    return _persist_server_entry(name, entry)


def _persist_server_entry(name: str, entry: dict) -> int:
    path, raw = _read_config_raw()
    mcp = _get_mcp_sections(raw)
    mcp[name] = entry
    _set_mcp_sections(raw, mcp)
    # Validate via the canonical parser before writing — catches any
    # shape issue (bad transport, missing fields, KISO_ env keys,
    # malformed cwd) at add time rather than at worker start.
    try:
        parse_mcp_section(mcp)
    except MCPConfigError as e:
        die(f"rejected server entry: {e}")
    _write_config_raw(path, raw)
    print(f"wrote [mcp.{name}] to {path}")
    return 0


def _parse_kv_pairs(raw: list[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            die(f"{flag} entry must be KEY=VAL, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            die(f"{flag} entry has empty key: {item!r}")
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# install --from-url
# ---------------------------------------------------------------------------


def _cmd_install(args: argparse.Namespace) -> int:
    # Pre-flight: check uv/npx on PATH. We only block when the
    # resolved plan actually needs the missing one; for now warn
    # upfront so the user has both tools available.
    missing = check_runtime_dependencies()
    try:
        resolved = resolve_from_url(args.from_url, name_hint=args.name)
    except InstallResolverError as e:
        die(str(e))

    if missing:
        tool_needs = _tools_required_by(resolved)
        blocking = [t for t in missing if t in tool_needs]
        if blocking:
            hint = {
                "uv": "https://docs.astral.sh/uv/",
                "npx": "https://nodejs.org/",
            }
            msgs = "\n  ".join(f"{t} → {hint[t]}" for t in blocking)
            die(
                f"missing required runtime(s):\n  {msgs}\n"
                f"install them and retry"
            )

    _print_install_plan(resolved)
    if args.dry_run:
        print("(dry run; no changes written)")
        return 0

    # Execute pre-install steps (git clone, uv venv, uv pip install)
    import subprocess as _sp
    for step in resolved.pre_install:
        print(f"+ {' '.join(step)}")
        result = _sp.run(step, capture_output=True, text=True)
        if result.returncode != 0:
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            die(f"pre-install step failed: {' '.join(step)}")

    entry = _entry_from_resolved(resolved)
    return _persist_server_entry(resolved.name, entry)


def _entry_from_resolved(res: ResolvedServer) -> dict:
    entry: dict = {"transport": res.transport}
    if res.transport == "stdio":
        entry["command"] = res.command
        if res.args:
            entry["args"] = list(res.args)
        if res.cwd:
            entry["cwd"] = res.cwd
        if res.env:
            entry["env"] = dict(res.env)
    else:
        entry["url"] = res.url
        if res.headers:
            entry["headers"] = dict(res.headers)
    return entry


def _tools_required_by(res: ResolvedServer) -> set[str]:
    needed: set[str] = set()
    cmd_tokens = [res.command or "", *res.args]
    for step in res.pre_install:
        cmd_tokens.extend(step)
    for tok in cmd_tokens:
        if tok in ("uv", "uvx"):
            needed.add("uv")
        if tok in ("npx",):
            needed.add("npx")
    return needed


def _print_install_plan(res: ResolvedServer) -> None:
    print(f"Resolved MCP server: {res.name}")
    print(f"  transport: {res.transport}")
    if res.transport == "stdio":
        print(f"  command: {res.command}")
        if res.args:
            print(f"  args: {res.args}")
        if res.cwd:
            print(f"  cwd: {res.cwd}")
    else:
        print(f"  url: {res.url}")
        if res.headers:
            print(f"  headers: {list(res.headers.keys())}")
    if res.pre_install:
        print("  pre-install steps:")
        for step in res.pre_install:
            print(f"    + {' '.join(step)}")
    for note in res.notes:
        print(f"  note: {note}")


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def _cmd_remove(args: argparse.Namespace) -> int:
    path, raw = _read_config_raw()
    mcp = _get_mcp_sections(raw)
    if args.name not in mcp:
        die(f"no such server: {args.name!r}")
    if not args.yes:
        print(f"About to remove [mcp.{args.name}] from {path}")
        confirm = input("confirm? [y/N] ").strip().lower()
        if confirm != "y":
            print("aborted")
            return 1
    del mcp[args.name]
    _set_mcp_sections(raw, mcp)
    _write_config_raw(path, raw)
    print(f"removed [mcp.{args.name}]")
    return 0


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def _cmd_test(args: argparse.Namespace) -> int:
    import asyncio

    from kiso.config import load_config
    from kiso.mcp.manager import MCPManager

    try:
        cfg = load_config()
    except SystemExit:
        raise

    if args.name not in cfg.mcp_servers:
        die(f"no such server: {args.name!r}")

    manager = MCPManager(cfg.mcp_servers)

    async def _run() -> int:
        try:
            methods = await manager.list_methods(args.name)
        except Exception as e:  # noqa: BLE001
            print(f"✗ test failed: {e}", file=sys.stderr)
            await manager.shutdown_all()
            return 1
        print(f"✓ initialize OK")
        print(f"✓ list_methods: {len(methods)} method(s)")
        for m in methods[:20]:
            summary = (m.description or "").split("\n")[0][:60]
            print(f"  - {m.qualified}  {summary}")
        if len(methods) > 20:
            print(f"  ... and {len(methods) - 20} more")
        await manager.shutdown_all()
        print(f"✓ shutdown OK")
        return 0

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def _cmd_logs(args: argparse.Namespace) -> int:
    log_path = MCP_LOG_DIR / f"{args.name}.err.log"
    if not log_path.exists():
        die(f"no log file for {args.name!r} at {log_path}")
    with open(log_path, "rb") as f:
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    for line in lines[-args.tail :]:
        print(line)
    return 0


# ---------------------------------------------------------------------------
# env (credential storage)
# ---------------------------------------------------------------------------


def _env_file(name: str) -> Path:
    MCP_ENV_DIR.mkdir(parents=True, exist_ok=True)
    return MCP_ENV_DIR / f"{name}.env"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v
    return out


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in sorted(values.items())]
    body = "\n".join(lines) + ("\n" if lines else "")
    path.write_text(body, encoding="utf-8")
    # 0600: readable/writable only by owner
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _cmd_env(args: argparse.Namespace) -> int:
    sub = getattr(args, "mcp_env_command", None)
    if sub is None:
        print("usage: kiso mcp env {set|unset|list|show} <name> ...")
        return 2
    name = args.name
    if not NAME_RE.match(name):
        die(f"invalid server name: {name!r}")
    path = _env_file(name)
    if sub == "set":
        if args.key.startswith("KISO_"):
            die("KISO_* variables cannot be set via kiso mcp env")
        values = _read_env_file(path)
        values[args.key] = args.value
        _write_env_file(path, values)
        print(f"set {args.key} in {path}")
        return 0
    if sub == "unset":
        values = _read_env_file(path)
        if args.key in values:
            del values[args.key]
            _write_env_file(path, values)
            print(f"unset {args.key} in {path}")
        else:
            print(f"{args.key} not set in {path}")
        return 0
    if sub == "list":
        values = _read_env_file(path)
        if not values:
            print("(no env vars set)")
            return 0
        for k in sorted(values.keys()):
            print(k)
        return 0
    if sub == "show":
        print(
            "WARNING: this prints secrets. Only run in a trusted terminal.",
            file=sys.stderr,
        )
        values = _read_env_file(path)
        for k in sorted(values.keys()):
            print(f"{k}={values[k]}")
        return 0
    die(f"unknown env subcommand: {sub}")
    return 2
