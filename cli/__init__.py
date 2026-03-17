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
        print("error: no 'cli' token in config.toml", file=sys.stderr)
        sys.exit(1)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiso", description=f"Kiso agent bot v{__version__}")
    parser.add_argument("-V", "--version", action=_VersionAction)

    # Chat-mode flags (top-level, not on a subcommand)
    parser.add_argument(
        "--session",
        default=None,
        help="session name (default: {hostname}@{user})",
    )
    parser.add_argument(
        "--api",
        default="http://localhost:8333",
        help="kiso server URL (default: http://localhost:8333)",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="user to send as (default: system user)",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="only show msg task content"
    )

    sub = parser.add_subparsers(dest="command")
    msg_parser = sub.add_parser("msg", help="send a message and print the response")
    msg_parser.add_argument("message", help="message text")
    def _add_tool_subcommands(parent_parser):
        """Add tool subcommands to a parser (shared by 'tool' and 'skill' alias)."""
        tool_sub = parent_parser.add_subparsers(dest="tool_command")
        tool_sub.add_parser("list", help="list installed tools")
        sp = tool_sub.add_parser("search", help="search official tools on GitHub")
        sp.add_argument("query", nargs="?", default="", help="search filter")
        ip = tool_sub.add_parser("install", help="install a tool")
        ip.add_argument("target", help="tool name or git URL")
        ip.add_argument("--name", default=None, help="custom install name")
        ip.add_argument("--no-deps", action="store_true", help="skip deps.sh")
        ip.add_argument("--show-deps", action="store_true", help="show deps.sh without installing")
        up = tool_sub.add_parser("update", help="update a tool")
        up.add_argument("target", help="tool name or 'all'")
        rp = tool_sub.add_parser("remove", help="remove a tool")
        rp.add_argument("name", help="tool name")
        tp = tool_sub.add_parser("test", help="run a tool's test suite")
        tp.add_argument("name", help="tool name")

    tool_parser = sub.add_parser("tool", help="manage tools")
    _add_tool_subcommands(tool_parser)

    # MD-based skill management
    skill_parser = sub.add_parser("skill", help="manage MD-based skills")
    skill_sub = skill_parser.add_subparsers(dest="skill_command")
    skill_sub.add_parser("list", help="list installed skills")
    si = skill_sub.add_parser("install", help="install a skill from a .md file")
    si.add_argument("source", help="path to .md skill file")
    sr = skill_sub.add_parser("remove", help="remove a skill")
    sr.add_argument("name", help="skill name")

    # Plugin umbrella command
    plugin_parser = sub.add_parser("plugin", help="unified plugin view")
    plugin_sub = plugin_parser.add_subparsers(dest="plugin_command")
    plugin_sub.add_parser("list", help="list all installed plugins")
    ps = plugin_sub.add_parser("search", help="search registry across all plugin types")
    ps.add_argument("query", nargs="?", default="", help="search filter")

    connector_parser = sub.add_parser("connector", help="manage connectors")
    connector_sub = connector_parser.add_subparsers(dest="connector_command")

    connector_sub.add_parser("list", help="list installed connectors")

    csearch_p = connector_sub.add_parser("search", help="search official connectors on GitHub")
    csearch_p.add_argument("query", nargs="?", default="", help="search filter")

    cinstall_p = connector_sub.add_parser("install", help="install a connector")
    cinstall_p.add_argument("target", help="connector name or git URL")
    cinstall_p.add_argument("--name", default=None, help="custom install name")
    cinstall_p.add_argument("--no-deps", action="store_true", help="skip deps.sh")
    cinstall_p.add_argument(
        "--show-deps", action="store_true", help="show deps.sh without installing"
    )

    cupdate_p = connector_sub.add_parser("update", help="update a connector")
    cupdate_p.add_argument("target", help="connector name or 'all'")

    cremove_p = connector_sub.add_parser("remove", help="remove a connector")
    cremove_p.add_argument("name", help="connector name")

    crun_p = connector_sub.add_parser("run", help="start a connector daemon")
    crun_p.add_argument("name", help="connector name")

    cstop_p = connector_sub.add_parser("stop", help="stop a connector daemon")
    cstop_p.add_argument("name", help="connector name")

    cstatus_p = connector_sub.add_parser("status", help="check connector status")
    cstatus_p.add_argument("name", help="connector name")

    ctest_p = connector_sub.add_parser("test", help="run a connector's test suite")
    ctest_p.add_argument("name", help="connector name")

    sessions_parser = sub.add_parser("sessions", help="list sessions")
    sessions_parser.add_argument(
        "--all", "-a", action="store_true", dest="show_all",
        help="show all sessions (admin only)",
    )

    env_parser = sub.add_parser("env", help="manage deploy secrets")
    env_sub = env_parser.add_subparsers(dest="env_command")

    env_set_p = env_sub.add_parser("set", help="set a deploy secret")
    env_set_p.add_argument("key", help="secret name")
    env_set_p.add_argument("value", help="secret value")

    env_get_p = env_sub.add_parser("get", help="get a deploy secret")
    env_get_p.add_argument("key", help="secret name")

    env_sub.add_parser("list", help="list deploy secret names")

    env_del_p = env_sub.add_parser("delete", help="delete a deploy secret")
    env_del_p.add_argument("key", help="secret name")

    env_sub.add_parser("reload", help="hot-reload .env into the server")

    user_parser = sub.add_parser("user", help="manage users")
    user_sub = user_parser.add_subparsers(dest="user_command")

    user_list_p = user_sub.add_parser("list", help="list all users")
    user_list_p.add_argument(
        "--json", action="store_true", dest="json",
        help="output as JSON (machine-readable)",
    )

    user_add_p = user_sub.add_parser("add", help="add a user")
    user_add_p.add_argument("username", help="user name")
    user_add_p.add_argument(
        "--role", required=True, choices=["admin", "user"], help="user role"
    )
    user_add_p.add_argument(
        "--skills",
        default=None,
        metavar="SKILLS",
        help="allowed skills: '*' or comma-separated names (required for role=user)",
    )
    user_add_p.add_argument(
        "--alias",
        action="append",
        metavar="CONNECTOR:ID",
        help="connector alias in 'connector:platform_id' format (repeatable)",
    )
    user_add_p.add_argument(
        "--no-reload", action="store_true", dest="no_reload",
        help="skip hot-reload after writing config (useful when server is not running)",
    )

    user_edit_p = user_sub.add_parser("edit", help="edit role or skills of an existing user")
    user_edit_p.add_argument("username", help="user to edit")
    user_edit_p.add_argument(
        "--role", default=None, choices=["admin", "user"], help="new role"
    )
    user_edit_p.add_argument(
        "--skills", default=None, metavar="SKILLS",
        help="new skills: '*' or comma-separated names",
    )
    user_edit_p.add_argument(
        "--no-reload", action="store_true", dest="no_reload",
        help="skip hot-reload after writing config",
    )

    user_remove_p = user_sub.add_parser("remove", help="remove a user")
    user_remove_p.add_argument("username", help="user to remove")
    user_remove_p.add_argument(
        "--no-reload", action="store_true", dest="no_reload",
        help="skip hot-reload after writing config",
    )

    user_alias_p = user_sub.add_parser("alias", help="manage connector aliases for a user")
    user_alias_p.add_argument("username", help="user name")
    user_alias_p.add_argument("--connector", required=True, help="connector name")
    user_alias_p.add_argument("--id", default=None, metavar="PLATFORM_ID", help="platform user ID")
    user_alias_p.add_argument("--remove", action="store_true", help="remove the alias")
    user_alias_p.add_argument(
        "--no-reload", action="store_true", dest="no_reload",
        help="skip hot-reload after writing config",
    )

    stats_p = sub.add_parser("stats", help="show token usage stats (admin only)")
    stats_p.add_argument("--since", type=int, default=30, metavar="N", help="look back N days (default: 30)")
    stats_p.add_argument("--session", default=None, metavar="NAME", help="filter by session name")
    stats_p.add_argument("--by", default="model", choices=["model", "session", "role"], help="group by dimension (default: model)")

    comp_p = sub.add_parser("completion", help="print shell completion script")
    comp_p.add_argument("shell", choices=["bash", "zsh"], help="target shell")

    version_p = sub.add_parser("version", help="print version and exit")
    version_p.add_argument("--stats", action="store_true", help="show line count breakdown")

    reset_parser = sub.add_parser("reset", help="reset/cleanup data")
    reset_sub = reset_parser.add_subparsers(dest="reset_command")

    rs = reset_sub.add_parser("session", help="reset one session")
    rs.add_argument("name", nargs="?", default=None, help="session name (default: current)")
    rs.add_argument("--yes", "-y", action="store_true", help="skip confirmation")

    rk = reset_sub.add_parser("knowledge", help="reset all knowledge")
    rk.add_argument("--yes", "-y", action="store_true", help="skip confirmation")

    ra = reset_sub.add_parser("all", help="reset all data")
    ra.add_argument("--yes", "-y", action="store_true", help="skip confirmation")

    rf = reset_sub.add_parser("factory", help="factory reset")
    rf.add_argument("--yes", "-y", action="store_true", help="skip confirmation")

    cancel_p = sub.add_parser("cancel", help="cancel the active job in a session")
    cancel_p.add_argument(
        "cancel_session", nargs="?", default=None,
        help="session to cancel (default: current session)",
    )

    # --- M413: rules subcommand ---
    rules_parser = sub.add_parser("rules", help="manage safety rules")
    rules_sub = rules_parser.add_subparsers(dest="rules_cmd")
    rules_sub.add_parser("list", help="list all safety rules")
    rules_add_p = rules_sub.add_parser("add", help="add a safety rule")
    rules_add_p.add_argument("rule_content", help="rule text")
    rules_rm_p = rules_sub.add_parser("remove", help="remove a safety rule by ID")
    rules_rm_p.add_argument("rule_id", type=int, help="rule ID to remove")

    # --- M673: knowledge subcommand ---
    know_parser = sub.add_parser("knowledge", help="manage knowledge facts")
    know_sub = know_parser.add_subparsers(dest="knowledge_cmd")
    know_list_p = know_sub.add_parser("list", help="list knowledge facts")
    know_list_p.add_argument("--category", "-c", help="filter by category")
    know_list_p.add_argument("--entity", "-e", help="filter by entity name")
    know_list_p.add_argument("--tag", "-t", help="filter by tag")
    know_list_p.add_argument("--limit", "-n", type=int, default=50, help="max results")
    know_add_p = know_sub.add_parser("add", help="add a knowledge fact")
    know_add_p.add_argument("content", help="fact text")
    know_add_p.add_argument("--category", "-c", default="general", help="fact category")
    know_add_p.add_argument("--entity", "-e", help="entity name")
    know_add_p.add_argument("--entity-kind", help="entity kind (default: concept)")
    know_add_p.add_argument("--tags", "-t", help="comma-separated tags")
    know_search_p = know_sub.add_parser("search", help="search knowledge")
    know_search_p.add_argument("query", help="search query")
    know_rm_p = know_sub.add_parser("remove", help="remove a knowledge fact by ID")
    know_rm_p.add_argument("fact_id", type=int, help="fact ID to remove")
    know_exp_p = know_sub.add_parser("export", help="export knowledge facts")
    know_exp_p.add_argument("--format", "-f", choices=["json", "md"], default="json", help="output format")
    know_exp_p.add_argument("--category", "-c", help="filter by category")
    know_exp_p.add_argument("--entity", "-e", help="filter by entity name")
    know_exp_p.add_argument("--output", "-o", help="output file (default: stdout)")
    know_imp_p = know_sub.add_parser("import", help="import knowledge from markdown file")
    know_imp_p.add_argument("file", help="markdown file path")
    know_imp_p.add_argument("--category", "-c", help="default category (default: general)")
    know_imp_p.add_argument("--dry-run", action="store_true", help="show what would be imported")

    # --- M681: cron subcommand ---
    cron_parser = sub.add_parser("cron", help="manage cron jobs")
    cron_sub = cron_parser.add_subparsers(dest="cron_cmd")
    cron_sub.add_parser("list", help="list cron jobs").add_argument(
        "--session", "-s", help="filter by session")
    cron_add_p = cron_sub.add_parser("add", help="add a cron job")
    cron_add_p.add_argument("schedule", help="cron expression (e.g. '0 9 * * *')")
    cron_add_p.add_argument("prompt", help="message to send on each trigger")
    cron_add_p.add_argument("--session", "-s", required=True, help="target session")
    cron_sub.add_parser("remove", help="remove a cron job").add_argument(
        "job_id", type=int, help="cron job ID")
    cron_sub.add_parser("enable", help="enable a cron job").add_argument(
        "job_id", type=int, help="cron job ID")
    cron_sub.add_parser("disable", help="disable a cron job").add_argument(
        "job_id", type=int, help="cron job ID")

    # --- M674: behavior subcommand ---
    beh_parser = sub.add_parser("behavior", help="manage behavioral guidelines")
    beh_sub = beh_parser.add_subparsers(dest="behavior_cmd")
    beh_sub.add_parser("list", help="list all behavioral guidelines")
    beh_add_p = beh_sub.add_parser("add", help="add a behavioral guideline")
    beh_add_p.add_argument("content", help="guideline text")
    beh_rm_p = beh_sub.add_parser("remove", help="remove a behavioral guideline by ID")
    beh_rm_p.add_argument("behavior_id", type=int, help="behavior ID to remove")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        _chat(args)
    elif args.command == "msg":
        _msg_cmd(args)
    elif args.command == "tool":
        from cli.tool import run_tool_command

        run_tool_command(args)
    elif args.command == "skill":
        from cli.skill import run_skill_command

        run_skill_command(args)
    elif args.command == "plugin":
        from cli.plugin import run_plugin_command

        run_plugin_command(args)
    elif args.command == "connector":
        from cli.connector import run_connector_command

        run_connector_command(args)
    elif args.command == "sessions":
        from cli.session import run_sessions_command

        run_sessions_command(args)
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
    elif args.command == "behavior":
        from cli.behavior import behavior_add, behavior_list, behavior_remove

        if args.behavior_cmd == "list" or args.behavior_cmd is None:
            behavior_list(args)
        elif args.behavior_cmd == "add":
            behavior_add(args)
        elif args.behavior_cmd == "remove":
            behavior_remove(args)
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
            print("error: server response missing message_id", file=sys.stderr)
            sys.exit(1)

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
        # M267: allow this role to show a new inflight indicator in future phases
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
    """Render an exec/skill/search/replan task: header, output, review."""

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
            # Plan goal updated (e.g. "Planning..." → real goal) — re-render header (M229)
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

        # M331: suppress pending task headers — only show when status transitions
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
                # M267: skip duplicate indicator for same role (e.g. planner validation retry)
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

        # M303/M306: show live partial content from streaming chunks.
        inflight_role = inflight.get("role") if inflight else None
        partial = ""
        if inflight_role in _STREAMING_VISIBLE_ROLES:
            partial = inflight.get("partial_content", "")
        if partial and len(partial) > state.partial_content_len:
            _clear_spinner()
            # M306: overwrite previous partial lines on TTY
            if caps.tty and state.partial_lines_rendered > 0:
                print(f"\033[{state.partial_lines_rendered}A\033[J", end="")
            rendered = render_partial_content(partial, caps)
            if rendered:
                print(rendered)
                state.partial_lines_rendered = rendered.count("\n") + 1
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
            Always pass an explicit value; the empty-string default is only
            provided to avoid breaking legacy test call sites.
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
