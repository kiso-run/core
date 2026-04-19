"""CLI entry point."""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
import typing

from kiso._version import __version__

from cli._http import _handle_http_error
from cli.render import (
    CLEAR_LINE,
    _format_resources,
    _parse_llm_calls,
    detect_caps,
    die,
    get_last_thinking,
    render_banner,
    render_cancel_start,
    render_command,
    render_inflight_indicator,
    render_llm_call_input_panel,
    render_llm_call_output_panel,
    render_llm_calls,
    render_msg_output,
    render_partial_content,
    render_phase_done,
    render_plan,
    render_plan_detail,
    render_planner_spinner,
    render_review,
    render_separator,
    render_task_header,
    render_task_output,
    render_usage,
    render_user_prompt,
    spinner_frames,
)

_STREAMING_VISIBLE_ROLES = frozenset({"messenger", "summarizer"})


class _VersionAction(argparse.Action):
    """Custom -V/--version action that prints version + total LOC and exits."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        if "--stats" in sys.argv:
            _print_version_stats()
        else:
            from pathlib import Path

            from kiso._version import count_loc

            root = Path(__file__).resolve().parent.parent
            total = count_loc(root)["total"]
            print(f"kiso {__version__}  ({_fmt_loc(total)} lines)")
        parser.exit()


class _ExitRepl(Exception):
    """Raised by /exit to break out of the REPL loop."""


_SLASH_COMMANDS = ["/clear", "/exit", "/help", "/sessions", "/stats", "/status", "/verbose-off", "/verbose-on"]

_verbose_mode = False


class _ClientContext(typing.NamedTuple):
    """Shared client context built by ``_setup_client_context``."""
    cfg: object
    caps: object
    client: object
    user: str
    session: str
    bot_name: str


def _setup_client_context(args: argparse.Namespace) -> _ClientContext:
    """Load config, build httpx client, resolve user/session."""
    import getpass
    import socket

    import httpx

    from kiso.config import load_config

    cfg = load_config()
    caps = detect_caps()
    token = cfg.tokens.get("cli")
    if not token:
        die("no 'cli' token in config.toml")

    user = args.user or getpass.getuser()
    session = args.session or f"{socket.gethostname()}@{user}"
    client = httpx.Client(
        base_url=args.api,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    bot_name = cfg.settings.get("bot_name", "Kiso")
    return _ClientContext(cfg=cfg, caps=caps, client=client, user=user, session=session, bot_name=bot_name)


def _setup_readline() -> None:
    """Configure tab-completion for slash commands and load persistent history."""
    try:
        import readline
    except ImportError:
        return

    def completer(text: str, state: int) -> str | None:
        if text.startswith("/"):
            matches = [c for c in _SLASH_COMMANDS if c.startswith(text)]
        else:
            matches = []
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")
    # Prevent pasted text (with trailing newline) from auto-submitting
    readline.parse_and_bind("set enable-bracketed-paste on")

    from kiso.config import KISO_DIR

    history_path = str(KISO_DIR / ".chat_history")
    try:
        readline.read_history_file(history_path)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(500)


def _save_readline_history() -> None:
    """Save readline history to persistent file."""
    try:
        import readline
    except ImportError:
        return
    from kiso.config import KISO_DIR

    try:
        KISO_DIR.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(KISO_DIR / ".chat_history"))
    except OSError:
        pass


def _add_connector_parser(sub) -> None:
    s = sub.add_parser("connector", help="manage connectors").add_subparsers(dest="connector_command")
    s.add_parser("list", help="list installed connectors")
    p = s.add_parser("search", help="search official connectors on GitHub")
    p.add_argument("query", nargs="?", default="", help="search filter")
    p = s.add_parser("install", help="install a connector")
    p.add_argument("target", help="connector name or git URL")
    p.add_argument("--name", default=None, help="custom install name")
    p.add_argument("--no-deps", action="store_true", help="skip deps.sh")
    p.add_argument("--show-deps", action="store_true", help="show deps.sh without installing")
    p.add_argument("--force", action="store_true",
                   help="force re-run deps.sh even if health_check passes")
    p = s.add_parser("update", help="update a connector")
    p.add_argument("target", help="connector name or 'all'")
    p = s.add_parser("remove", help="remove a connector")
    p.add_argument("name", help="connector name")
    p = s.add_parser("run", help="start a connector daemon")
    p.add_argument("name", help="connector name")
    p = s.add_parser("stop", help="stop a connector daemon")
    p.add_argument("name", help="connector name")
    p = s.add_parser("status", help="check connector status")
    p.add_argument("name", help="connector name")
    p = s.add_parser("test", help="run a connector's test suite")
    p.add_argument("name", help="connector name")


def _add_user_parser(sub) -> None:
    s = sub.add_parser("user", help="manage users").add_subparsers(dest="user_command")
    p = s.add_parser("list", help="list all users")
    p.add_argument("--json", action="store_true", dest="json", help="output as JSON (machine-readable)")
    p = s.add_parser("add", help="add a user")
    p.add_argument("username", help="user name")
    p.add_argument("--role", required=True, choices=["admin", "user"], help="user role")
    p.add_argument("--wrappers", default=None, metavar="SKILLS",
                   help="allowed wrappers: '*' or comma-separated names (required for role=user)")
    p.add_argument("--alias", action="append", metavar="CONNECTOR:ID",
                   help="connector alias in 'connector:platform_id' format (repeatable)")
    p.add_argument("--no-reload", action="store_true", dest="no_reload",
                   help="skip hot-reload after writing config (useful when server is not running)")
    p = s.add_parser("edit", help="edit role or wrappers of an existing user")
    p.add_argument("username", help="user to edit")
    p.add_argument("--role", default=None, choices=["admin", "user"], help="new role")
    p.add_argument("--wrappers", default=None, metavar="SKILLS", help="new wrappers: '*' or comma-separated names")
    p.add_argument("--no-reload", action="store_true", dest="no_reload", help="skip hot-reload after writing config")
    p = s.add_parser("remove", help="remove a user")
    p.add_argument("username", help="user to remove")
    p.add_argument("--no-reload", action="store_true", dest="no_reload", help="skip hot-reload after writing config")
    p = s.add_parser("alias", help="manage connector aliases for a user")
    p.add_argument("username", help="user name")
    p.add_argument("--connector", required=True, help="connector name")
    p.add_argument("--id", default=None, metavar="PLATFORM_ID", help="platform user ID")
    p.add_argument("--remove", action="store_true", help="remove the alias")
    p.add_argument("--no-reload", action="store_true", dest="no_reload", help="skip hot-reload after writing config")


def _add_knowledge_parser(sub) -> None:
    s = sub.add_parser("knowledge", help="manage knowledge facts").add_subparsers(dest="knowledge_cmd")
    p = s.add_parser("list", help="list knowledge facts")
    p.add_argument("--category", "-c", help="filter by category")
    p.add_argument("--entity", "-e", help="filter by entity name")
    p.add_argument("--tag", "-t", help="filter by tag")
    p.add_argument("--limit", "-n", type=int, default=50, help="max results")
    p = s.add_parser("add", help="add a knowledge fact")
    p.add_argument("content", help="fact text")
    p.add_argument("--category", "-c", default="general", help="fact category")
    p.add_argument("--entity", "-e", help="entity name")
    p.add_argument("--entity-kind", help="entity kind (default: concept)")
    p.add_argument("--tags", "-t", help="comma-separated tags")
    p = s.add_parser("search", help="search knowledge")
    p.add_argument("query", help="search query")
    p = s.add_parser("remove", help="remove a knowledge fact by ID")
    p.add_argument("fact_id", type=int, help="fact ID to remove")
    p = s.add_parser("export", help="export knowledge facts")
    p.add_argument("--format", "-f", choices=["json", "md"], default="json", help="output format")
    p.add_argument("--category", "-c", help="filter by category")
    p.add_argument("--entity", "-e", help="filter by entity name")
    p.add_argument("--output", "-o", help="output file (default: stdout)")
    p = s.add_parser("import", help="import knowledge from markdown file")
    p.add_argument("file", help="markdown file path")
    p.add_argument("--category", "-c", help="default category (default: general)")
    p.add_argument("--dry-run", action="store_true", help="show what would be imported")


def _add_project_parser(sub) -> None:
    s = sub.add_parser("project", help="manage projects").add_subparsers(dest="project_cmd")
    s.add_parser("list", help="list projects")
    p = s.add_parser("create", help="create a project")
    p.add_argument("name", help="project name")
    p.add_argument("--description", "-d", help="project description")
    p = s.add_parser("show", help="show project details")
    p.add_argument("name", help="project name")
    p = s.add_parser("bind", help="bind session to project")
    p.add_argument("session", help="session ID")
    p.add_argument("project", help="project name")
    p = s.add_parser("unbind", help="unbind session from project")
    p.add_argument("session", help="session ID")
    p.add_argument("project", help="project name")
    p = s.add_parser("add-member", help="add member to project")
    p.add_argument("username", help="username")
    p.add_argument("--project", "-p", required=True, help="project name")
    p.add_argument("--role", "-r", choices=["member", "viewer"], default="member", help="role")
    p = s.add_parser("remove-member", help="remove member from project")
    p.add_argument("username", help="username")
    p.add_argument("--project", "-p", required=True, help="project name")
    p = s.add_parser("members", help="list project members")
    p.add_argument("--project", "-p", required=True, help="project name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiso", description=f"Kiso agent bot v{__version__}")
    parser.add_argument("-V", "--version", action=_VersionAction)
    parser.add_argument("--session", default=None, help="session name (default: {hostname}@{user})")
    parser.add_argument("--api", default="http://localhost:8333", help="kiso server URL (default: http://localhost:8333)")
    parser.add_argument("--user", default=None, help="user to send as (default: system user)")
    parser.add_argument("--quiet", "-q", action="store_true", help="only show msg task content")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("msg", help="send a message and print the response").add_argument("message", help="message text")

    # Role
    role_sub = sub.add_parser(
        "role", help="manage role files in ~/.kiso/roles/",
    ).add_subparsers(dest="role_command")
    role_sub.add_parser("list", help="list user vs package roles")
    rp = role_sub.add_parser(
        "reset", help="overwrite a role file with the package version",
    )
    rp.add_argument("name", nargs="?", default=None,
                    help="role name to reset (omit with --all)")
    rp.add_argument("--all", action="store_true",
                    help="reset every package role")
    rp.add_argument("--yes", "-y", action="store_true",
                    help="skip confirmation for non-empty existing files")

    # Roles — plural form is the canonical surface; singular
    # `kiso role` above is preserved for one cycle as a deprecated alias.
    roles_sub = sub.add_parser(
        "roles", help="discover, inspect, and reset role files",
    ).add_subparsers(dest="roles_command")
    roles_sub.add_parser("list", help="list every role with model and override status")
    p = roles_sub.add_parser("show", help="print the resolved prompt for a role")
    p.add_argument("name", help="role name")
    p = roles_sub.add_parser("diff", help="diff a user override against the bundled default")
    p.add_argument("name", help="role name")
    p = roles_sub.add_parser("reset", help="overwrite a user override with the bundled default")
    p.add_argument("name", nargs="?", default=None,
                   help="role name to reset (omit with --all)")
    p.add_argument("--all", action="store_true",
                   help="reset every package role")
    p.add_argument("--yes", "-y", action="store_true",
                   help="skip confirmation for non-empty existing files")

    # Plugin umbrella
    ps = sub.add_parser("plugin", help="unified plugin view").add_subparsers(dest="plugin_command")
    ps.add_parser("list", help="list all installed plugins")
    p = ps.add_parser("search", help="search registry across all plugin types")
    p.add_argument("query", nargs="?", default="", help="search filter")

    _add_connector_parser(sub)
    _add_user_parser(sub)
    _add_knowledge_parser(sub)
    _add_project_parser(sub)

    # Sessions
    p = sub.add_parser("sessions", help="list sessions")
    p.add_argument("--all", "-a", action="store_true", dest="show_all", help="show all sessions (admin only)")
    ss = sub.add_parser("session", help="manage sessions").add_subparsers(dest="session_cmd")
    p = ss.add_parser("create", help="create a named session")
    p.add_argument("name", help="session name")
    p.add_argument("--description", "-d", help="session description")

    # Env
    es = sub.add_parser("env", help="manage deploy secrets").add_subparsers(dest="env_command")
    p = es.add_parser("set", help="set a deploy secret")
    p.add_argument("key", help="secret name")
    p.add_argument("value", help="secret value")
    p = es.add_parser("get", help="get a deploy secret")
    p.add_argument("key", help="secret name")
    es.add_parser("list", help="list deploy secret names")
    p = es.add_parser("delete", help="delete a deploy secret")
    p.add_argument("key", help="secret name")
    es.add_parser("reload", help="hot-reload .env into the server")

    # Stats
    p = sub.add_parser("stats", help="show token usage stats (admin only)")
    p.add_argument("--since", type=int, default=30, metavar="N", help="look back N days (default: 30)")
    p.add_argument("--session", default=None, metavar="NAME", help="filter by session name")
    p.add_argument("--by", default="model", choices=["model", "session", "role"], help="group by dimension (default: model)")

    sub.add_parser("completion", help="print shell completion script").add_argument("shell", choices=["bash", "zsh"], help="target shell")
    sub.add_parser("version", help="print version and exit").add_argument("--stats", action="store_true", help="show line count breakdown")

    # Reset
    rs = sub.add_parser("reset", help="reset/cleanup data").add_subparsers(dest="reset_command")
    for name, help_text in [("session", "reset one session"), ("knowledge", "reset all knowledge"),
                            ("all", "reset all data"), ("factory", "factory reset")]:
        p = rs.add_parser(name, help=help_text)
        p.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
        if name == "session":
            p.add_argument("name", nargs="?", default=None, help="session name (default: current)")

    # Cancel
    p = sub.add_parser("cancel", help="cancel the active job in a session")
    p.add_argument("cancel_session", nargs="?", default=None, help="session to cancel (default: current session)")

    # Rules
    rs = sub.add_parser("rules", help="manage safety rules").add_subparsers(dest="rules_cmd")
    rs.add_parser("list", help="list all safety rules")
    p = rs.add_parser("add", help="add a safety rule")
    p.add_argument("rule_content", help="rule text")
    p = rs.add_parser("remove", help="remove a safety rule by ID")
    p.add_argument("rule_id", type=int, help="rule ID to remove")

    # Cron
    cs = sub.add_parser("cron", help="manage cron jobs").add_subparsers(dest="cron_cmd")
    cs.add_parser("list", help="list cron jobs").add_argument("--session", "-s", help="filter by session")
    p = cs.add_parser("add", help="add a cron job")
    p.add_argument("schedule", help="cron expression (e.g. '0 9 * * *')")
    p.add_argument("prompt", help="message to send on each trigger")
    p.add_argument("--session", "-s", required=True, help="target session")
    cs.add_parser("remove", help="remove a cron job").add_argument("job_id", type=int, help="cron job ID")
    cs.add_parser("enable", help="enable a cron job").add_argument("job_id", type=int, help="cron job ID")
    cs.add_parser("disable", help="disable a cron job").add_argument("job_id", type=int, help="cron job ID")

    # Behavior
    bs = sub.add_parser("behavior", help="manage behavioral guidelines").add_subparsers(dest="behavior_cmd")
    bs.add_parser("list", help="list all behavioral guidelines")
    p = bs.add_parser("add", help="add a behavioral guideline")
    p.add_argument("content", help="guideline text")
    p = bs.add_parser("remove", help="remove a behavioral guideline by ID")
    p.add_argument("behavior_id", type=int, help="behavior ID to remove")

    # Config
    cfg_sub = sub.add_parser("config", help="manage settings").add_subparsers(dest="config_cmd")
    p = cfg_sub.add_parser("set", help="set a config value")
    p.add_argument("key", help="setting name")
    p.add_argument("value", help="new value")
    p = cfg_sub.add_parser("get", help="get a config value")
    p.add_argument("key", help="setting name")
    cfg_sub.add_parser("list", help="list all settings")

    # Preset
    ps = sub.add_parser("preset", help="manage persona presets").add_subparsers(dest="preset_cmd")
    ps.add_parser("list", help="list available presets from registry")
    p = ps.add_parser("search", help="search presets")
    p.add_argument("query", help="search query")
    p = ps.add_parser("install", help="install a preset")
    p.add_argument("target", help="preset name or local path")
    p.add_argument("--dry-run", action="store_true", help="show what would be installed")
    p = ps.add_parser("show", help="show preset details")
    p.add_argument("name", help="preset name or local path")
    ps.add_parser("installed", help="list installed presets")
    p = ps.add_parser("remove", help="remove an installed preset")
    p.add_argument("name", help="preset name")

    # MCP — Model Context Protocol client (consumer-only)
    from cli.mcp import add_subcommands as _add_mcp_subcommands

    mcp = sub.add_parser("mcp", help="manage MCP servers (consumer-only)")
    _add_mcp_subcommands(mcp)

    # Init — create ~/.kiso/config.toml from a bundled preset
    init_p = sub.add_parser(
        "init",
        help="bootstrap ~/.kiso/config.toml (optionally from a preset)",
    )
    init_p.add_argument(
        "--preset",
        default="default",
        help="preset to apply (default: 'default'; use 'none' for empty mcp block)",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing config.toml",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        _chat(args)
    elif args.command == "msg":
        _msg_cmd(args)
    elif args.command == "mcp":
        from cli.mcp import handle as run_mcp_command

        sys.exit(run_mcp_command(args))
    elif args.command == "init":
        from cli.init import run_init_command

        sys.exit(run_init_command(args))
    elif args.command == "role":
        from cli.role import run_role_command

        run_role_command(args)
    elif args.command == "roles":
        from cli.roles import run_roles_command

        run_roles_command(args)
    elif args.command == "plugin":
        from cli.plugin import run_plugin_command

        run_plugin_command(args)
    elif args.command == "connector":
        from cli.connector import run_connector_command

        run_connector_command(args)
    elif args.command == "sessions":
        from cli.session import run_sessions_command

        run_sessions_command(args)
    elif args.command == "session":
        from cli.session import session_create

        if args.session_cmd == "create":
            session_create(args)
    elif args.command == "env":
        from cli.env import run_env_command

        run_env_command(args)
    elif args.command == "user":
        from cli.user import run_user_command

        run_user_command(args)
    elif args.command == "reset":
        from cli.reset import run_reset_command

        run_reset_command(args)
    elif args.command == "stats":
        from cli.stats import run_stats_command

        run_stats_command(args)
    elif args.command == "completion":
        import importlib.resources
        script = importlib.resources.files("kiso") / "completions" / f"kiso.{args.shell}"
        print(script.read_text(encoding="utf-8"), end="")
    elif args.command == "cancel":
        _cancel_cmd(args)
    elif args.command == "rules":
        from cli.rules import rules_add, rules_list, rules_remove

        if args.rules_cmd == "list" or args.rules_cmd is None:
            rules_list(args)
        elif args.rules_cmd == "add":
            rules_add(args)
        elif args.rules_cmd == "remove":
            rules_remove(args)
    elif args.command == "knowledge":
        from cli.knowledge import (
            knowledge_add, knowledge_export, knowledge_import, knowledge_list,
            knowledge_remove, knowledge_search,
        )

        if args.knowledge_cmd == "list" or args.knowledge_cmd is None:
            knowledge_list(args)
        elif args.knowledge_cmd == "add":
            knowledge_add(args)
        elif args.knowledge_cmd == "search":
            knowledge_search(args)
        elif args.knowledge_cmd == "remove":
            knowledge_remove(args)
        elif args.knowledge_cmd == "export":
            knowledge_export(args)
        elif args.knowledge_cmd == "import":
            knowledge_import(args)
    elif args.command == "cron":
        from cli.cron import cron_add, cron_disable, cron_enable, cron_list, cron_remove

        if args.cron_cmd == "list" or args.cron_cmd is None:
            cron_list(args)
        elif args.cron_cmd == "add":
            cron_add(args)
        elif args.cron_cmd == "remove":
            cron_remove(args)
        elif args.cron_cmd == "enable":
            cron_enable(args)
        elif args.cron_cmd == "disable":
            cron_disable(args)
    elif args.command == "project":
        from cli.project import (
            project_add_member, project_bind, project_create, project_list,
            project_members, project_remove_member, project_show, project_unbind,
        )

        if args.project_cmd == "list" or args.project_cmd is None:
            project_list(args)
        elif args.project_cmd == "create":
            project_create(args)
        elif args.project_cmd == "show":
            project_show(args)
        elif args.project_cmd == "bind":
            project_bind(args)
        elif args.project_cmd == "unbind":
            project_unbind(args)
        elif args.project_cmd == "add-member":
            project_add_member(args)
        elif args.project_cmd == "remove-member":
            project_remove_member(args)
        elif args.project_cmd == "members":
            project_members(args)
    elif args.command == "behavior":
        from cli.behavior import behavior_add, behavior_list, behavior_remove

        if args.behavior_cmd == "list" or args.behavior_cmd is None:
            behavior_list(args)
        elif args.behavior_cmd == "add":
            behavior_add(args)
        elif args.behavior_cmd == "remove":
            behavior_remove(args)
    elif args.command == "preset":
        from cli.preset import (
            preset_install, preset_installed, preset_list, preset_remove,
            preset_search, preset_show,
        )

        if args.preset_cmd == "list" or args.preset_cmd is None:
            preset_list(args)
        elif args.preset_cmd == "search":
            preset_search(args)
        elif args.preset_cmd == "install":
            preset_install(args)
        elif args.preset_cmd == "show":
            preset_show(args)
        elif args.preset_cmd == "installed":
            preset_installed(args)
        elif args.preset_cmd == "remove":
            preset_remove(args)
    elif args.command == "config":
        from cli.config_cmd import run_config_command
        run_config_command(args)
    elif args.command == "version":
        if getattr(args, "stats", False):
            _print_version_stats()
        else:
            from pathlib import Path

            from kiso._version import count_loc

            root = Path(__file__).resolve().parent.parent
            total = count_loc(root)["total"]
            print(f"kiso {__version__}  ({_fmt_loc(total)} lines)")


def _cancel_cmd(args: argparse.Namespace) -> None:
    """Cancel the active job in a session.

    Usage:
        kiso cancel            Cancel current session's job
        kiso cancel <session>  Cancel a specific session's job

    Programmatic use:
        Wrappers and bots should prefer the REST API directly:
        POST /sessions/{sid}/cancel
        No --all flag in core; cancel-all is a wrapper concern.
    """
    import httpx

    ctx = _setup_client_context(args)
    session = args.cancel_session or ctx.session
    try:
        resp = ctx.client.post(f"/sessions/{session}/cancel")
        resp.raise_for_status()
    except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
        _handle_http_error(exc, args.api)
    finally:
        ctx.client.close()

    data = resp.json()
    if data.get("cancelled"):
        print(f"Job cancelled in session {session} (plan {data.get('plan_id')})")
        drained = data.get("drained", 0)
        if drained:
            print(f"  {drained} queued message(s) drained")
    else:
        print("No active job to cancel.", file=sys.stderr)
        sys.exit(1)


def _fmt_loc(n: int) -> str:
    return str(n)


def _print_version_stats() -> None:
    """Print kiso version followed by a LOC breakdown per area."""
    from pathlib import Path

    from kiso._version import count_loc

    root = Path(__file__).resolve().parent.parent
    stats = count_loc(root)

    rows = [
        ("core", stats["core"], "kiso/"),
        ("cli",  stats["cli"],  "cli/"),
    ]
    num_w = max(len(_fmt_loc(v)) for v in stats.values())
    print(f"kiso {__version__}\n")
    for label, n, path in rows:
        print(f"  {label:<5}  {_fmt_loc(n):>{num_w}} lines   ({path})")
    print(f"  {'─' * (num_w + 12)}")
    print(f"  {'total':<5}  {_fmt_loc(stats['total']):>{num_w}} lines")


def _msg_cmd(args: argparse.Namespace) -> None:
    """Send a single message and print the response (non-interactive)."""
    import httpx

    ctx = _setup_client_context(args)
    # Implicitly quiet when not a TTY
    quiet = args.quiet or not ctx.caps.tty

    try:
        try:
            resp = ctx.client.post(
                "/msg",
                json={"session": ctx.session, "user": ctx.user, "content": args.message},
            )
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            _handle_http_error(exc, args.api)

        data = resp.json()
        if data.get("untrusted"):
            print("warning: message was not trusted by the server.", file=sys.stderr)
            sys.exit(1)

        message_id = data.get("message_id")
        if message_id is None:
            die("server response missing message_id")

        _poll_status(ctx.client, ctx.session, message_id, 0, quiet, False, ctx.caps, ctx.bot_name, user=ctx.user)
    except KeyboardInterrupt:
        try:
            ctx.client.post(f"/sessions/{ctx.session}/cancel")
        except httpx.HTTPError:
            pass
        sys.exit(130)
    finally:
        ctx.client.close()


def _chat(args: argparse.Namespace) -> None:
    import httpx


    ctx = _setup_client_context(args)
    prompt = render_user_prompt(ctx.user, ctx.caps)

    # Fetch resource info for banner (best-effort)
    resources = None
    try:
        resp = ctx.client.get("/health")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            res = data.get("resources")
            if isinstance(res, dict):
                resources = res
    except Exception:
        pass
    print(render_banner(ctx.bot_name, ctx.session, ctx.caps, __version__, resources=resources))

    # Tab-completion for slash commands
    _setup_readline()

    last_task_id = 0
    try:
        while True:
            try:
                text = input(f"{prompt} ")
            except (KeyboardInterrupt, EOFError):
                print()
                break
            text = text.strip()
            if not text:
                continue
            if text.startswith("/"):
                try:
                    _handle_slash(text, ctx.client, ctx.session, ctx.user, ctx.caps, ctx.bot_name)
                except _ExitRepl:
                    break
                continue
            if text in ("exit", "quit"):
                break

            try:
                resp = ctx.client.post(
                    "/msg",
                    json={"session": ctx.session, "user": ctx.user, "content": text},
                )
                resp.raise_for_status()
            except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
                _handle_http_error(exc, args.api, fatal=False)
                continue

            data = resp.json()
            if data.get("untrusted"):
                print("warning: message was not trusted by the server.")
                continue

            message_id = data.get("message_id")
            if message_id is None:
                print("warning: server response missing message_id, skipping poll")
                continue

            try:
                last_task_id = _poll_status(
                    ctx.client, ctx.session, message_id, last_task_id,
                    args.quiet, _verbose_mode, ctx.caps, ctx.bot_name, user=ctx.user,
                )
            except KeyboardInterrupt:
                print(f"\n{render_cancel_start(ctx.caps)}")
                try:
                    ctx.client.post(f"/sessions/{ctx.session}/cancel")
                except httpx.HTTPError:
                    pass
    finally:
        _save_readline_history()
        ctx.client.close()


_POLL_EVERY = 2  # poll API every 2 iterations (2 × 80ms ≈ 160ms)


@dataclasses.dataclass
class _PollRenderState:
    """Mutable rendering state threaded through ``_poll_status``."""
    seen: dict  # tid → (status, review_verdict, substatus, llm_call_count)
    max_task_id: int = 0
    shown_plan_id: int | None = None
    shown_plan_goal: str = ""
    shown_plan_llm_count: int = 0
    shown_plan_verbose_count: int = 0
    active_spinner_task: dict | None = None
    active_spinner_index: int = 0
    active_spinner_total: int = 0
    planning_phase: bool = False
    worker_phase: str = "idle"  # last known worker phase from /status
    phase_start: float = 0.0  # time.monotonic() when current phase began
    seen_any_task: bool = False
    spinner_active: bool = False
    spinner_start: float = 0.0  # time.monotonic() when current spinner started
    at_col0: bool = True
    verbose_shown: dict = None  # tid → number of verbose LLM calls already rendered
    seen_inflight_ts: set = dataclasses.field(default_factory=set)  # timestamps of rendered inflight indicators
    inflight_roles_shown: set = dataclasses.field(default_factory=set)  # roles with active (unresolved) inflight indicators
    inflight_in_shown: set = dataclasses.field(default_factory=set)  # ts values whose IN panel was rendered from inflight
    partial_content_len: int = 0  # length of partial_content already printed
    partial_lines_rendered: int = 0  # lines of partial output on screen (for ANSI overwrite)


def _print_verbose_panels(calls: list[dict], caps, state: _PollRenderState) -> None:
    """Print input+output panels for *calls* as paired IN→OUT."""
    for c in calls:
        # Skip IN panel if already rendered from inflight data
        ts = c.get("ts")
        if ts and ts in state.inflight_in_shown:
            state.inflight_in_shown.discard(ts)
        else:
            in_panel = render_llm_call_input_panel(c, caps)
            if in_panel:
                print(in_panel)
        out_panel = render_llm_call_output_panel(c, caps)
        if out_panel:
            print(out_panel)
        # allow this role to show a new inflight indicator in future phases
        role = c.get("role")
        if role:
            state.inflight_roles_shown.discard(role)


def _emit_verbose_calls(task: dict, caps, state: _PollRenderState, llm_call_count: int) -> None:
    """Render only the verbose LLM panels not yet shown for *task*."""
    tid = task["id"]
    already = state.verbose_shown.get(tid, 0)

    calls = _parse_llm_calls(task.get("llm_calls"))
    verbose_calls = [c for c in calls if c.get("messages")]
    new_calls = verbose_calls[already:]

    if new_calls:
        _print_verbose_panels(new_calls, caps, state)

    state.verbose_shown[tid] = len(verbose_calls)


def _should_stop_polling(
    plan: dict | None,
    tasks: list,
    message_id: int,
    worker_running: bool,
    seen: dict,
    failed_stable_polls: int,
) -> tuple[bool, int]:
    """Determine if polling should stop after a status fetch.

    Returns ``(should_break, new_failed_stable_polls)``.
    """
    if (
        plan
        and plan.get("message_id") == message_id
        and plan.get("status") not in ("running", "replanning")
    ):
        if plan.get("status") in ("failed", "done") and worker_running:
            # Worker still running after plan ended — could be a replan
            # (reviewer-triggered "failed" or self-directed "done").
            # Wait until all tasks are rendered, then break after a
            # short stability window (~5s) to catch the new plan.
            all_rendered = tasks and all(seen.get(t["id"]) is not None for t in tasks)
            if all_rendered:
                failed_stable_polls += 1
                if failed_stable_polls >= 30:  # ~5s (30 × 160ms)
                    return True, failed_stable_polls
            else:
                failed_stable_polls = 0
        else:
            return True, 0
    else:
        failed_stable_polls = 0
    return False, failed_stable_polls


def _render_msg_task(
    task: dict,
    quiet: bool,
    verbose: bool,
    caps,
    bot_name: str,
    state: _PollRenderState,
    idx: int,
    total: int,
) -> None:
    """Render a msg task: header + spinner when running, header + output when done."""

    status = task["status"]
    output = task.get("output", "") or ""

    if quiet:
        if status == "done":
            print(render_msg_output(output, caps, bot_name))
            print(render_separator(caps))
        return

    if status == "done":
        if state.seen_any_task:
            print()
        state.seen_any_task = True
        llm_calls_raw = task.get("llm_calls")
        llm_detail = render_llm_calls(llm_calls_raw, caps)
        if llm_detail:
            print(llm_detail)
        if verbose:
            _emit_verbose_calls(task, caps, state, len(_parse_llm_calls(llm_calls_raw)))
        print(render_msg_output(output, caps, bot_name, thinking=get_last_thinking(llm_calls_raw)))
        print(render_separator(caps))
    elif status == "running":
        # Show header on first transition to running
        if state.active_spinner_task is not task:
            if state.seen_any_task:
                print()
            state.seen_any_task = True
            print(render_task_header(task, idx, total, caps))
            state.spinner_start = time.monotonic()
        state.active_spinner_task = task
        state.active_spinner_index = idx
        state.active_spinner_total = total


def _render_other_task(
    task: dict,
    quiet: bool,
    verbose: bool,
    caps,
    state: _PollRenderState,
    idx: int,
    total: int,
) -> None:
    """Render an exec/wrapper/search/replan task: header, output, review."""

    if quiet:
        return

    status = task["status"]
    output = task.get("output", "") or ""

    if state.seen_any_task:
        print()
    state.seen_any_task = True
    print(render_task_header(task, idx, total, caps))

    task_command = task.get("command")
    if task_command:
        print(render_command(task_command, caps))

    if status in ("done", "failed"):
        stderr_text = task.get("stderr", "") or ""
        if status == "failed" and stderr_text:
            display_output = f"{output}\n{stderr_text}".strip() if output else stderr_text
        else:
            display_output = output
    else:
        display_output = ""
    if display_output:
        out = render_task_output(display_output, caps)
        if out:
            print(out)

    review_line = render_review(task, caps)
    if review_line:
        print(review_line)

    if verbose and status in ("done", "failed"):
        _emit_verbose_calls(task, caps, state, len(_parse_llm_calls(task.get("llm_calls"))))

    if status == "running":
        if state.active_spinner_task is not task:
            state.spinner_start = time.monotonic()
        state.active_spinner_task = task
        state.active_spinner_index = idx
        state.active_spinner_total = total


def _write_spinner_line(line: str, state: _PollRenderState) -> None:
    """Write a spinner line with CLEAR_LINE, handling newline prefix."""
    if not state.spinner_active and not state.at_col0:
        sys.stdout.write('\n')
    sys.stdout.write(f"\r{CLEAR_LINE}{line}")
    sys.stdout.flush()
    state.spinner_active = True
    state.at_col0 = False


def _render_plan_header(goal: str, task_count: int, caps, tasks: list, replan: bool = False) -> None:
    """Render plan goal + detail + separators (used 3x in plan status)."""
    print(f"\n{render_plan(goal, task_count, caps, replan=replan)}")
    plan_detail = render_plan_detail(tasks, caps)
    if plan_detail:
        print(render_separator(caps))
        print(plan_detail)
    print(render_separator(caps))


def _render_plan_status(
    data: dict,
    message_id: int,
    quiet: bool,
    verbose: bool,
    caps,
    bot_name: str,
    state: _PollRenderState,
) -> list:
    """Render plan + task updates to the terminal. Mutates *state* in-place.

    Returns the filtered task list for the current plan (used by the caller
    to drive stop-condition checks via ``_should_stop_polling``).
    """

    plan = data.get("plan")
    all_tasks = data.get("tasks", [])
    worker_running = data.get("worker_running", False)

    current_plan_id = (
        plan["id"] if plan and plan.get("message_id") == message_id else None
    )
    tasks = [
        t for t in all_tasks
        if current_plan_id is not None and t.get("plan_id") == current_plan_id
    ]

    def _clear_spinner() -> None:
        if (state.active_spinner_task or state.planning_phase) and caps.tty:
            sys.stdout.write(f"\r{CLEAR_LINE}")
            sys.stdout.flush()
            state.active_spinner_task = None
            state.planning_phase = False
            state.spinner_active = False
            state.at_col0 = True

    # Show plan goal / replan detection (only for current message)
    if plan and not quiet and plan.get("message_id") == message_id:
        pid = plan["id"]
        goal = plan["goal"]
        task_count = len(tasks)
        if state.shown_plan_id is None:
            _clear_spinner()
            _render_plan_header(goal, task_count, caps, tasks)
            state.shown_plan_id = pid
            state.shown_plan_goal = goal
        elif pid == state.shown_plan_id and goal != state.shown_plan_goal and task_count > 0:
            # Plan goal updated (e.g. "Planning..." → real goal) — re-render header
            _clear_spinner()
            _render_plan_header(goal, task_count, caps, tasks)
            state.shown_plan_goal = goal
        elif pid != state.shown_plan_id:
            _clear_spinner()
            _render_plan_header(goal, task_count, caps, tasks, replan=True)
            state.shown_plan_id = pid
            state.shown_plan_goal = goal
            state.seen.clear()
            state.verbose_shown.clear()
            state.shown_plan_llm_count = 0
            state.shown_plan_verbose_count = 0
            state.seen_any_task = False

    # Show plan-level LLM calls incrementally (classifier appears early, planner later)
    if plan and not quiet and plan.get("message_id") == message_id and plan.get("llm_calls"):
        plan_calls = _parse_llm_calls(plan.get("llm_calls"))
        call_count = len(plan_calls)
        if call_count > state.shown_plan_llm_count:
            _clear_spinner()
            if verbose:
                verbose_calls = [c for c in plan_calls if c.get("messages")]
                new_calls = verbose_calls[state.shown_plan_verbose_count:]
                if new_calls:
                    _print_verbose_panels(new_calls, caps, state)
                    state.shown_plan_verbose_count = len(verbose_calls)
            # Show summary line only when plan is no longer running
            if plan.get("status") not in ("running", "replanning"):
                llm_detail = render_llm_calls(plan.get("llm_calls"), caps)
                if llm_detail:
                    print(llm_detail)
            state.shown_plan_llm_count = call_count

    total = len(tasks)

    # Render tasks that changed status or review
    for idx, task in enumerate(tasks, 1):
        tid = task["id"]
        status = task["status"]
        review_verdict = task.get("review_verdict")
        llm_call_count = len(_parse_llm_calls(task.get("llm_calls")))
        substatus = task.get("substatus") or ""
        task_key = (status, review_verdict, substatus, llm_call_count)

        if tid > state.max_task_id:
            state.max_task_id = tid
        if state.seen.get(tid) == task_key:
            continue

        # suppress pending task headers — only show when status transitions
        if status == "pending":
            state.seen[tid] = task_key
            continue

        prev_key = state.seen.get(tid)
        state.seen[tid] = task_key
        prev_status = prev_key[0] if prev_key else None
        ttype = task.get("type", "")
        output = task.get("output", "") or ""

        # Clear spinner line before printing new content
        _clear_spinner()

        # If only review/llm_calls changed (status unchanged),
        # show just the review line without re-rendering the task
        if prev_status == status and prev_status is not None:
            if not quiet:
                # msg tasks don't show review lines (output is rendered separately)
                if ttype != "msg":
                    prev_review = prev_key[1] if prev_key else None
                    if review_verdict != prev_review:
                        review_line = render_review(task, caps)
                        if review_line:
                            print(review_line)
                # Verbose panels for ALL task types (including msg)
                if verbose:
                    _emit_verbose_calls(task, caps, state, llm_call_count)
            continue

        if ttype == "msg" and status != "skipped":
            _render_msg_task(task, quiet, verbose, caps, bot_name, state, idx, total)
            continue

        _render_other_task(task, quiet, verbose, caps, state, idx, total)

    # Track worker phase from server status
    new_phase = data.get("worker_phase", "idle")
    old_phase = state.worker_phase
    if new_phase != old_phase:
        # Emit completion line for the previous phase
        if old_phase != "idle" and state.phase_start and not quiet:
            elapsed = time.monotonic() - state.phase_start
            done_line = render_phase_done(old_phase, elapsed, caps)
            if done_line:
                _clear_spinner()
                print(done_line)
        state.worker_phase = new_phase
        state.phase_start = time.monotonic()
        if new_phase != "idle":
            state.spinner_start = time.monotonic()

    # Update planning_phase: spinner shown while worker is thinking but no
    # task is executing.
    any_task_running = any(t.get("status") == "running" for t in tasks)
    has_matching_running_plan = (
        plan
        and plan.get("message_id") == message_id
        and plan.get("status") in ("running", "replanning")
    )
    has_matching_plan = bool(plan and plan.get("message_id") == message_id)
    was_planning = state.planning_phase
    state.planning_phase = bool(
        (has_matching_running_plan and not any_task_running and not state.active_spinner_task)
        or (worker_running and not has_matching_plan)
    )
    if state.planning_phase and not was_planning:
        state.spinner_start = time.monotonic()

    # Render inflight LLM call: show full IN panel so the user can see
    # what was sent while the model is thinking.  When the completed
    # llm_calls entry arrives later, _print_verbose_panels will skip the
    # IN panel (already shown) and render only the OUT panel.
    if verbose and not quiet:
        inflight = data.get("inflight_call")
        if inflight and inflight.get("messages"):
            inflight_ts = inflight.get("ts")
            inflight_role = inflight.get("role") or None
            if inflight_ts and inflight_ts not in state.seen_inflight_ts:
                state.seen_inflight_ts.add(inflight_ts)
                # skip duplicate indicator for same role (e.g. planner validation retry)
                if inflight_role is None or inflight_role not in state.inflight_roles_shown:
                    _clear_spinner()
                    in_panel = render_llm_call_input_panel(inflight, caps)
                    if in_panel:
                        print(in_panel)
                        state.inflight_in_shown.add(inflight_ts)
                    else:
                        print(render_inflight_indicator(inflight, caps))
                    if inflight_role is not None:
                        state.inflight_roles_shown.add(inflight_role)

        # show live partial content from streaming chunks.
        inflight_role = inflight.get("role") if inflight else None
        partial = ""
        if inflight_role in _STREAMING_VISIBLE_ROLES:
            partial = inflight.get("partial_content", "")
        if partial and len(partial) > state.partial_content_len:
            _clear_spinner()
            # overwrite previous partial lines on TTY
            if caps.tty and state.partial_lines_rendered > 0:
                print(f"\033[{state.partial_lines_rendered}A\033[J", end="")
            rendered, visual_lines = render_partial_content(partial, caps)
            if rendered:
                print(rendered)
                state.partial_lines_rendered = visual_lines
            state.partial_content_len = len(partial)
        elif not inflight:
            # Call completed — reset partial tracking for next call
            state.partial_content_len = 0
            state.partial_lines_rendered = 0

    # Restore spinner for running tasks after inflight/phase rendering
    # may have cleared it via _clear_spinner().
    if not state.active_spinner_task:
        for idx, task in enumerate(tasks, 1):
            if task.get("status") == "running":
                state.active_spinner_task = task
                state.active_spinner_index = idx
                state.active_spinner_total = len(tasks)
                break

    return tasks


def _poll_status(
    client: "httpx.Client",
    session: str,
    message_id: int,
    base_task_id: int,
    quiet: bool,
    verbose: bool,
    caps: "TermCaps",  # noqa: F821
    bot_name: str = "Bot",
    _at_col0: bool = True,
    user: str = "",
) -> int:
    """Poll ``/status`` and render task progress to the terminal.

    Args:
        client: authenticated httpx.Client pointed at the kiso server.
        session: session identifier to poll.
        message_id: id of the user message that triggered the plan.
        base_task_id: only consider tasks with id > this value.
        quiet: suppress all output except final message text.
        verbose: show per-LLM-call token panels.
        caps: terminal capabilities (color, unicode, TTY, size).
        bot_name: display name used in message output headers.
        _at_col0: ``True`` (default) if the cursor is at column 0 when this
            function is called.  Pass ``False`` when the caller has printed
            text without a trailing newline (e.g. an inline prompt) so the
            spinner always opens on a fresh line instead of overwriting it.
        user: user forwarded as the ``user`` query param to ``/status``.
    """

    state = _PollRenderState(seen={}, max_task_id=base_task_id, at_col0=_at_col0, verbose_shown={})
    counter = 0
    no_plan_since_worker_stopped = 0
    failed_stable_polls = 0
    frames = spinner_frames(caps)

    while True:
        if counter % _POLL_EVERY == 0:
            try:
                resp = client.get(
                    f"/status/{session}",
                    params={"after": base_task_id, "verbose": str(verbose).lower(), "user": user},
                )
                resp.raise_for_status()
            except Exception:
                time.sleep(0.08)
                counter += 1
                continue

            data = resp.json()
            plan = data.get("plan")
            worker_running = data.get("worker_running", False)
            tasks = _render_plan_status(data, message_id, quiet, verbose, caps, bot_name, state)

            should_break, failed_stable_polls = _should_stop_polling(
                plan, tasks, message_id, worker_running, state.seen, failed_stable_polls
            )

            if should_break:
                if state.active_spinner_task and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                    state.spinner_active = False
                    state.at_col0 = True
                # Emit final phase completion line
                if not quiet and state.worker_phase != "idle" and state.phase_start:
                    elapsed = time.monotonic() - state.phase_start
                    done_line = render_phase_done(state.worker_phase, elapsed, caps)
                    if done_line:
                        print(done_line)
                if not quiet:
                    usage_line = render_usage(plan, caps)
                    if usage_line:
                        print(usage_line)
                break

            # Fallback: worker stopped without creating a plan for this message
            has_matching_plan = plan and plan.get("message_id") == message_id
            if not worker_running and not has_matching_plan:
                no_plan_since_worker_stopped += 1
                if no_plan_since_worker_stopped >= 3:
                    if state.active_spinner_task and caps.tty:
                        sys.stdout.write(f"\r{CLEAR_LINE}")
                        sys.stdout.flush()
                        state.spinner_active = False
                        state.at_col0 = True
                    print("error: worker stopped without producing a result", file=sys.stderr)
                    break
            else:
                no_plan_since_worker_stopped = 0

        # Animate spinner on TTY
        if caps.tty:
            frame = frames[counter % len(frames)]
            elapsed = int(time.monotonic() - state.spinner_start) if state.spinner_start else 0
            if state.active_spinner_task:
                line = render_task_header(
                    state.active_spinner_task, state.active_spinner_index,
                    state.active_spinner_total, caps, spinner_frame=frame,
                    elapsed=elapsed,
                )
                _write_spinner_line(line, state)
            elif state.planning_phase:
                phase = state.worker_phase if state.worker_phase != "idle" else "planning"
                line = render_planner_spinner(caps, frame, elapsed=elapsed, phase=phase)
                _write_spinner_line(line, state)
            else:
                state.spinner_active = False

        time.sleep(0.08)
        counter += 1

    return state.max_task_id


def _handle_slash(
    text: str, client, session: str, user: str,
    caps: "TermCaps", bot_name: str,  # noqa: F821
) -> None:
    """Handle a slash command. Raises _ExitRepl on /exit."""
    parts = text.split(None, 1)
    cmd = parts[0].lower()

    if cmd == "/exit":
        raise _ExitRepl

    elif cmd == "/help":
        _slash_help(caps)

    elif cmd == "/status":
        _slash_status(client, session, user, caps)

    elif cmd == "/sessions":
        _slash_sessions(client, user, caps)

    elif cmd == "/stats":
        _slash_stats(client, user, session, caps)

    elif cmd == "/verbose-on":
        global _verbose_mode
        _verbose_mode = True
        print("Verbose mode: ON — LLM calls will show full input/output")

    elif cmd == "/verbose-off":
        _verbose_mode = False
        print("Verbose mode: OFF")

    elif cmd == "/clear":
        if caps.tty:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    else:
        print(f"Unknown command: {cmd}. Type /help for available commands.")


def _slash_help(caps: "TermCaps") -> None:  # noqa: F821
    """Print available REPL slash commands."""

    print(render_separator(caps))
    print("  /help        — show this help")
    print("  /status      — server health + session info")
    print("  /sessions    — list your sessions")
    print("  /stats       — token usage for this session (last 7 days)")
    print("  /verbose-on  — show full LLM input/output")
    print("  /verbose-off — hide LLM details (default)")
    print("  /clear       — clear the screen")
    print("  /exit        — exit the REPL")
    print(render_separator(caps))


def _slash_status(client, session: str, user: str, caps: "TermCaps") -> None:  # noqa: F821
    """Show server health, session message count, and worker state."""
    import httpx


    print(render_separator(caps))

    # Health + resources
    try:
        resp = client.get("/health")
        resp.raise_for_status()
        health_data = resp.json()
        print(f"  Health: {health_data.get('status', 'ok')}")
        resources = health_data.get("resources")
        if resources:
            res_line = _format_resources(resources, caps)
            if res_line:
                print(res_line)
    except (httpx.HTTPError, httpx.ConnectError):
        print("  Health: unreachable")

    # Session info (message count)
    try:
        resp = client.get(f"/sessions/{session}/info")
        resp.raise_for_status()
        info = resp.json()
        print(f"  Session: {info.get('session', session)}")
        print(f"  Messages: {info.get('message_count', '?')}")
    except (httpx.HTTPError, httpx.ConnectError):
        print(f"  Session: {session} (info unavailable)")

    # Worker status
    try:
        resp = client.get(f"/status/{session}", params={"user": user})
        resp.raise_for_status()
        data = resp.json()
        running = "running" if data.get("worker_running") else "idle"
        print(f"  Worker: {running}")
        print(f"  Queue: {data.get('queue_length', 0)}")
    except (httpx.HTTPError, httpx.ConnectError):
        print("  Worker: unknown")

    print(render_separator(caps))


def _slash_sessions(client, user: str, caps: "TermCaps") -> None:  # noqa: F821
    """List sessions for the current user."""
    import httpx

    from cli.session import _relative_time

    print(render_separator(caps))

    try:
        resp = client.get("/sessions", params={"user": user})
        resp.raise_for_status()
    except (httpx.ConnectError, httpx.HTTPError) as exc:
        _handle_http_error(exc, "server", fatal=False)
        print(render_separator(caps))
        return

    sessions = resp.json()
    if not sessions:
        print("  No sessions found.")
        print(render_separator(caps))
        return

    max_name = max(len(s["session"]) for s in sessions)
    for s in sessions:
        name = s["session"].ljust(max_name)
        parts = []
        if s.get("connector"):
            parts.append(f"connector: {s['connector']}")
        parts.append(f"last activity: {_relative_time(s.get('updated_at'))}")
        print(f"  {name}  — {', '.join(parts)}")

    print(render_separator(caps))


def _slash_stats(client, user: str, session: str, caps: "TermCaps") -> None:  # noqa: F821
    """Show token usage for the current session (last 7 days)."""
    import httpx

    from cli.stats import print_stats

    print(render_separator(caps))
    try:
        resp = client.get(
            "/admin/stats",
            params={"user": user, "since": 7, "session": session, "by": "model"},
        )
        resp.raise_for_status()
        print_stats(resp.json())
    except httpx.ConnectError:
        print("  [stats unavailable — cannot connect]")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            print("  [stats unavailable — admin access required]")
        else:
            print(f"  [stats error: {exc.response.status_code}]")
    print(render_separator(caps))


if __name__ == "__main__":
    main()
