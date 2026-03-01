"""CLI entry point."""

from __future__ import annotations

import argparse
import dataclasses
import sys
import typing

from kiso._version import __version__


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
    from cli.render import detect_caps

    cfg = load_config()
    caps = detect_caps()
    token = cfg.tokens.get("cli")
    if not token:
        print("error: no 'cli' token in config.toml")
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
        help="session name (default: {hostname}@{username})",
    )
    parser.add_argument(
        "--api",
        default="http://localhost:8333",
        help="kiso server URL (default: http://localhost:8333)",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="username to send as (default: system user)",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="only show msg task content"
    )

    sub = parser.add_subparsers(dest="command")
    msg_parser = sub.add_parser("msg", help="send a message and print the response")
    msg_parser.add_argument("message", help="message text")
    skill_parser = sub.add_parser("skill", help="manage skills")
    skill_sub = skill_parser.add_subparsers(dest="skill_command")

    skill_sub.add_parser("list", help="list installed skills")

    search_p = skill_sub.add_parser("search", help="search official skills on GitHub")
    search_p.add_argument("query", nargs="?", default="", help="search filter")

    install_p = skill_sub.add_parser("install", help="install a skill")
    install_p.add_argument("target", help="skill name or git URL")
    install_p.add_argument("--name", default=None, help="custom install name")
    install_p.add_argument("--no-deps", action="store_true", help="skip deps.sh")
    install_p.add_argument(
        "--show-deps", action="store_true", help="show deps.sh without installing"
    )

    update_p = skill_sub.add_parser("update", help="update a skill")
    update_p.add_argument("target", help="skill name or 'all'")

    remove_p = skill_sub.add_parser("remove", help="remove a skill")
    remove_p.add_argument("name", help="skill name")

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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        _chat(args)
    elif args.command == "msg":
        _msg_cmd(args)
    elif args.command == "skill":
        from cli.skill import run_skill_command

        run_skill_command(args)
    elif args.command == "connector":
        from cli.connector import run_connector_command

        run_connector_command(args)
    elif args.command == "sessions":
        from cli.session import run_sessions_command

        run_sessions_command(args)
    elif args.command == "env":
        from cli.env import run_env_command

        run_env_command(args)
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
    elif args.command == "version":
        if getattr(args, "stats", False):
            _print_version_stats()
        else:
            from pathlib import Path

            from kiso._version import count_loc

            root = Path(__file__).resolve().parent.parent
            total = count_loc(root)["total"]
            print(f"kiso {__version__}  ({_fmt_loc(total)} lines)")


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
        resp = ctx.client.post(
            "/msg",
            json={"session": ctx.session, "user": ctx.user, "content": args.message},
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        print(f"error: cannot connect to {args.api}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        print(f"error: {exc.response.status_code} — {exc.response.text}", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    if data.get("untrusted"):
        print("warning: message was not trusted by the server.", file=sys.stderr)
        ctx.client.close()
        sys.exit(1)

    message_id = data["message_id"]

    try:
        _poll_status(ctx.client, ctx.session, message_id, 0, quiet, False, ctx.caps, ctx.bot_name)
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

    from cli.render import render_banner, render_cancel_start, render_user_prompt

    ctx = _setup_client_context(args)
    prompt = render_user_prompt(ctx.user, ctx.caps)
    print(render_banner(ctx.bot_name, ctx.session, ctx.caps, __version__))

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
            except httpx.ConnectError:
                print(f"error: cannot connect to {args.api}")
                continue
            except httpx.HTTPStatusError as exc:
                print(f"error: {exc.response.status_code} — {exc.response.text}")
                continue

            data = resp.json()
            if data.get("untrusted"):
                print("warning: message was not trusted by the server.")
                continue

            message_id = data["message_id"]

            try:
                last_task_id = _poll_status(
                    ctx.client, ctx.session, message_id, last_task_id,
                    args.quiet, _verbose_mode, ctx.caps, ctx.bot_name,
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
    shown_plan_llm_id: int | None = None
    active_spinner_task: dict | None = None
    active_spinner_index: int = 0
    active_spinner_total: int = 0
    planning_phase: bool = False
    seen_any_task: bool = False
    spinner_active: bool = False
    at_col0: bool = True


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
        if plan.get("status") == "failed" and worker_running:
            # Worker still running after plan failed — could be replan or
            # post-plan processing.  Wait until all tasks are rendered, then
            # break after a short stability window to catch replans.
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
    import json as _json

    from cli.render import (
        CLEAR_LINE,
        render_command,
        render_llm_calls,
        render_llm_calls_verbose,
        render_msg_output,
        render_plan,
        render_plan_detail,
        render_review,
        render_separator,
        render_task_header,
        render_task_output,
    )

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
        task_count = len(tasks)
        if state.shown_plan_id is None:
            _clear_spinner()
            print(f"\n{render_plan(plan['goal'], task_count, caps)}")
            plan_detail = render_plan_detail(tasks, caps)
            if plan_detail:
                print(render_separator(caps))
                print(plan_detail)
            print(render_separator(caps))
            state.shown_plan_id = pid
        elif pid != state.shown_plan_id:
            _clear_spinner()
            print(f"\n{render_plan(plan['goal'], task_count, caps, replan=True)}")
            plan_detail = render_plan_detail(tasks, caps)
            if plan_detail:
                print(render_separator(caps))
                print(plan_detail)
            print(render_separator(caps))
            state.shown_plan_id = pid
            state.seen.clear()
            state.seen_any_task = False

    # Show plan-level LLM calls (planner step) once available
    if (plan and not quiet and plan.get("message_id") == message_id
            and plan.get("llm_calls")
            and state.shown_plan_llm_id != plan.get("id")):
        _clear_spinner()
        llm_detail = render_llm_calls(plan.get("llm_calls"), caps)
        if llm_detail:
            print(llm_detail)
        if verbose:
            verbose_detail = render_llm_calls_verbose(plan.get("llm_calls"), caps)
            if verbose_detail:
                print(verbose_detail)
        state.shown_plan_llm_id = plan["id"]

    total = len(tasks)

    # Render tasks that changed status or review
    for idx, task in enumerate(tasks, 1):
        tid = task["id"]
        status = task["status"]
        review_verdict = task.get("review_verdict")
        llm_call_count = len(_json.loads(task["llm_calls"])) if task.get("llm_calls") else 0
        substatus = task.get("substatus") or ""
        task_key = (status, review_verdict, substatus, llm_call_count)

        if tid > state.max_task_id:
            state.max_task_id = tid
        if state.seen.get(tid) == task_key:
            continue

        prev_key = state.seen.get(tid)
        state.seen[tid] = task_key
        prev_status = prev_key[0] if prev_key else None
        ttype = task.get("type", "")
        output = task.get("output", "") or ""

        # Clear spinner line before printing new content
        _clear_spinner()

        if quiet:
            if ttype == "msg" and status == "done":
                print(render_msg_output(output, caps, bot_name))
                print(render_separator(caps))
            continue

        # If only review/llm_calls changed (status unchanged),
        # show just the review line without re-rendering the task
        if prev_status == status and prev_status is not None:
            if ttype != "msg":
                review_line = render_review(task, caps)
                if review_line:
                    print(review_line)
                if verbose:
                    verbose_detail = render_llm_calls_verbose(task.get("llm_calls"), caps)
                    if verbose_detail:
                        print(verbose_detail)
            continue

        # Msg tasks: only show via render_msg_output when done
        if ttype == "msg":
            if status == "done":
                llm_detail = render_llm_calls(task.get("llm_calls"), caps)
                if llm_detail:
                    print(llm_detail)
                if verbose:
                    verbose_detail = render_llm_calls_verbose(task.get("llm_calls"), caps)
                    if verbose_detail:
                        print(verbose_detail)
                print(render_msg_output(output, caps, bot_name))
                print(render_separator(caps))
            elif status == "running":
                state.active_spinner_task = task
                state.active_spinner_index = idx
                state.active_spinner_total = total
            continue

        # Print header for non-msg tasks (blank line between tasks)
        if state.seen_any_task:
            print()
        state.seen_any_task = True
        print(render_task_header(task, idx, total, caps))

        # Show translated command for exec tasks
        task_command = task.get("command")
        if task_command:
            print(render_command(task_command, caps))

        # Show output for completed/failed tasks
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

        # Show review verdict for completed exec/skill tasks
        review_line = render_review(task, caps)
        if review_line:
            print(review_line)

        # Verbose LLM detail AFTER compact review summary
        if verbose and status in ("done", "failed"):
            verbose_detail = render_llm_calls_verbose(task.get("llm_calls"), caps)
            if verbose_detail:
                print(verbose_detail)

        # Track running task for spinner
        if status == "running":
            state.active_spinner_task = task
            state.active_spinner_index = idx
            state.active_spinner_total = total

    # Update planning_phase: spinner shown while worker is thinking but no
    # task is executing.
    any_task_running = any(t.get("status") == "running" for t in tasks)
    has_matching_running_plan = (
        plan
        and plan.get("message_id") == message_id
        and plan.get("status") in ("running", "replanning")
    )
    has_matching_plan = bool(plan and plan.get("message_id") == message_id)
    state.planning_phase = bool(
        (has_matching_running_plan and not any_task_running and not state.active_spinner_task)
        or (worker_running and not has_matching_plan)
    )

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
    """
    import time

    from cli.render import (
        CLEAR_LINE,
        render_planner_spinner,
        render_task_header,
        render_usage,
        spinner_frames,
    )

    _MAX_POLL_SECONDS = 300  # safety net: 5 min max

    state = _PollRenderState(seen={}, max_task_id=base_task_id, at_col0=_at_col0)
    counter = 0
    start_time = time.time()
    no_plan_since_worker_stopped = 0
    failed_stable_polls = 0
    frames = spinner_frames(caps)

    while True:
        if counter % _POLL_EVERY == 0:
            try:
                resp = client.get(
                    f"/status/{session}",
                    params={"after": base_task_id, "verbose": str(verbose).lower()},
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
                    print("error: worker stopped without producing a result")
                    break
            else:
                no_plan_since_worker_stopped = 0

            # Safety net: absolute timeout
            if time.time() - start_time > _MAX_POLL_SECONDS:
                if state.active_spinner_task and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                    state.spinner_active = False
                    state.at_col0 = True
                print("error: timed out waiting for response")
                break

        # Animate spinner on TTY
        if caps.tty:
            frame = frames[counter % len(frames)]
            if state.active_spinner_task:
                line = render_task_header(
                    state.active_spinner_task, state.active_spinner_index,
                    state.active_spinner_total, caps, spinner_frame=frame,
                )
                if not state.spinner_active and not state.at_col0:
                    sys.stdout.write('\n')
                sys.stdout.write(f"\r{CLEAR_LINE}{line}")
                sys.stdout.flush()
                state.spinner_active = True
                state.at_col0 = False
            elif state.planning_phase:
                line = render_planner_spinner(caps, frame)
                if not state.spinner_active and not state.at_col0:
                    sys.stdout.write('\n')
                sys.stdout.write(f"\r{CLEAR_LINE}{line}")
                sys.stdout.flush()
                state.spinner_active = True
                state.at_col0 = False
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
        _slash_status(client, session, caps)

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
    from cli.render import render_separator

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


def _slash_status(client, session: str, caps: "TermCaps") -> None:  # noqa: F821
    """Show server health, session message count, and worker state."""
    import httpx

    from cli.render import render_separator

    print(render_separator(caps))

    # Health
    try:
        resp = client.get("/health")
        resp.raise_for_status()
        print(f"  Health: {resp.json().get('status', 'ok')}")
    except (httpx.HTTPError, httpx.ConnectError):
        print("  Health: unreachable")

    # Session info (message count)
    try:
        resp = client.get(f"/sessions/{session}/info")
        resp.raise_for_status()
        info = resp.json()
        print(f"  Session: {info['session']}")
        print(f"  Messages: {info['message_count']}")
    except (httpx.HTTPError, httpx.ConnectError):
        print(f"  Session: {session} (info unavailable)")

    # Worker status
    try:
        resp = client.get(f"/status/{session}")
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
    from cli.render import render_separator

    print(render_separator(caps))

    try:
        resp = client.get("/sessions", params={"user": user})
        resp.raise_for_status()
    except httpx.ConnectError:
        print("  error: cannot connect to server")
        print(render_separator(caps))
        return
    except httpx.HTTPError as exc:
        print(f"  error: {exc}")
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

    from cli.render import render_separator
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
