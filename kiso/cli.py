"""CLI entry point."""

from __future__ import annotations

import argparse
import sys


class _ExitRepl(Exception):
    """Raised by /exit to break out of the REPL loop."""


_SLASH_COMMANDS = ["/clear", "/exit", "/help", "/sessions", "/status"]


def _setup_readline() -> None:
    """Configure tab-completion for slash commands."""
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kiso", description="Kiso agent bot")

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
    sub.add_parser("serve", help="start the HTTP server")
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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        _chat(args)
    elif args.command == "msg":
        _msg_cmd(args)
    elif args.command == "serve":
        _serve()
    elif args.command == "skill":
        from kiso.cli_skill import run_skill_command

        run_skill_command(args)
    elif args.command == "connector":
        from kiso.cli_connector import run_connector_command

        run_connector_command(args)
    elif args.command == "sessions":
        from kiso.cli_session import run_sessions_command

        run_sessions_command(args)
    elif args.command == "env":
        from kiso.cli_env import run_env_command

        run_env_command(args)


def _msg_cmd(args: argparse.Namespace) -> None:
    """Send a single message and print the response (non-interactive)."""
    import getpass
    import socket

    import httpx

    from kiso.config import load_config
    from kiso.render import detect_caps

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
    # Implicitly quiet when not a TTY
    quiet = args.quiet or not caps.tty

    try:
        resp = client.post(
            "/msg",
            json={"session": session, "user": user, "content": args.message},
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
        client.close()
        sys.exit(1)

    message_id = data["message_id"]

    try:
        _poll_status(client, session, message_id, 0, quiet, caps, bot_name)
    except KeyboardInterrupt:
        try:
            client.post(f"/sessions/{session}/cancel")
        except httpx.HTTPError:
            pass
        sys.exit(130)
    finally:
        client.close()


def _chat(args: argparse.Namespace) -> None:
    import getpass
    import socket

    import httpx

    from kiso.config import load_config
    from kiso.render import detect_caps, render_banner, render_cancel_start, render_user_prompt

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
    prompt = render_user_prompt(user, caps)
    print(render_banner(bot_name, session, caps))

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
                    _handle_slash(text, client, session, user, caps, bot_name)
                except _ExitRepl:
                    break
                continue
            if text in ("exit", "quit"):
                break

            try:
                resp = client.post(
                    "/msg",
                    json={"session": session, "user": user, "content": text},
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
                    client, session, message_id, last_task_id,
                    args.quiet, caps, bot_name,
                )
            except KeyboardInterrupt:
                print(f"\n{render_cancel_start(caps)}")
                try:
                    client.post(f"/sessions/{session}/cancel")
                except httpx.HTTPError:
                    pass
    finally:
        client.close()


_POLL_EVERY = 6  # poll API every 6 iterations (6 × 80ms ≈ 480ms)


def _poll_status(
    client: "httpx.Client",
    session: str,
    message_id: int,
    base_task_id: int,
    quiet: bool,
    caps: "TermCaps",  # noqa: F821
    bot_name: str = "Bot",
) -> int:
    import time

    from kiso.render import (
        CLEAR_LINE,
        render_command,
        render_msg_output,
        render_plan,
        render_plan_detail,
        render_planner_spinner,
        render_review,
        render_separator,
        render_task_header,
        render_task_output,
        render_usage,
        spinner_frames,
    )

    _MAX_POLL_SECONDS = 300  # safety net: 5 min max

    seen: dict[int, str] = {}
    shown_plan_id: int | None = None
    max_task_id = base_task_id
    counter = 0
    start_time = time.time()
    no_plan_since_worker_stopped = 0  # consecutive polls with no plan and no worker
    frames = spinner_frames(caps)
    active_spinner_task: dict | None = None
    active_spinner_index: int = 0
    active_spinner_total: int = 0
    planning_phase = False
    seen_any_task = False  # for blank-line spacing between tasks

    while True:
        if counter % _POLL_EVERY == 0:
            try:
                resp = client.get(
                    f"/status/{session}", params={"after": base_task_id}
                )
                resp.raise_for_status()
            except Exception:
                time.sleep(0.08)
                counter += 1
                continue

            data = resp.json()
            plan = data.get("plan")
            all_tasks = data.get("tasks", [])

            # Only consider tasks belonging to the current plan
            current_plan_id = (
                plan["id"]
                if plan and plan.get("message_id") == message_id
                else None
            )
            tasks = [
                t for t in all_tasks
                if current_plan_id is not None and t.get("plan_id") == current_plan_id
            ]

            # Show plan goal / replan detection (only for current message)
            if plan and not quiet and plan.get("message_id") == message_id:
                pid = plan["id"]
                task_count = len(tasks)
                if shown_plan_id is None:
                    # Clear spinner line before printing plan
                    if (active_spinner_task or planning_phase) and caps.tty:
                        sys.stdout.write(f"\r{CLEAR_LINE}")
                        sys.stdout.flush()
                        active_spinner_task = None
                        planning_phase = False
                    print(f"\n{render_plan(plan['goal'], task_count, caps)}")
                    plan_detail = render_plan_detail(tasks, caps)
                    if plan_detail:
                        print(render_separator(caps))
                        print(plan_detail)
                    print(render_separator(caps))
                    shown_plan_id = pid
                elif pid != shown_plan_id:
                    if (active_spinner_task or planning_phase) and caps.tty:
                        sys.stdout.write(f"\r{CLEAR_LINE}")
                        sys.stdout.flush()
                        active_spinner_task = None
                        planning_phase = False
                    print(f"\n{render_plan(plan['goal'], task_count, caps, replan=True)}")
                    plan_detail = render_plan_detail(tasks, caps)
                    if plan_detail:
                        print(render_separator(caps))
                        print(plan_detail)
                    print(render_separator(caps))
                    shown_plan_id = pid
                    seen.clear()
                    seen_any_task = False

            total = len(tasks)

            # Render tasks that changed status
            for idx, task in enumerate(tasks, 1):
                tid = task["id"]
                status = task["status"]
                if tid > max_task_id:
                    max_task_id = tid
                if seen.get(tid) == status:
                    continue
                seen[tid] = status
                ttype = task.get("type", "")
                output = task.get("output", "") or ""

                # Clear spinner line before printing new content
                if (active_spinner_task or planning_phase) and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                    active_spinner_task = None
                    planning_phase = False

                if quiet:
                    if ttype == "msg" and status == "done":
                        print(render_msg_output(output, caps, bot_name))
                        print(render_separator(caps))
                    continue

                # Msg tasks: only show via render_msg_output when done
                if ttype == "msg":
                    if status == "done":
                        print(render_msg_output(output, caps, bot_name))
                        print(render_separator(caps))
                    continue

                # Print header for non-msg tasks (blank line between tasks)
                if seen_any_task:
                    print()
                seen_any_task = True
                print(render_task_header(task, idx, total, caps))

                # Show translated command for exec tasks
                task_command = task.get("command")
                if task_command:
                    print(render_command(task_command, caps))

                # Show output for completed/failed tasks
                display_output = output or (task.get("stderr", "") or "") if status in ("done", "failed") else ""
                if display_output:
                    out = render_task_output(display_output, caps)
                    if out:
                        print(out)

                # Show review verdict for completed exec/skill tasks
                review_line = render_review(task, caps)
                if review_line:
                    print(review_line)

                # Track running task for spinner
                if status == "running":
                    active_spinner_task = task
                    active_spinner_index = idx
                    active_spinner_total = total

            # Detect planning phase: plan running, no task executing yet
            has_matching_running_plan = (
                plan
                and plan.get("message_id") == message_id
                and plan.get("status") == "running"
            )
            any_task_running = any(t.get("status") == "running" for t in tasks)
            planning_phase = bool(
                has_matching_running_plan and not any_task_running and not active_spinner_task
            )

            # Done condition: plan matches our message and is no longer running
            worker_running = data.get("worker_running", False)
            if (
                plan
                and plan.get("message_id") == message_id
                and plan.get("status") != "running"
            ):
                # Don't exit on "failed" if worker is still running (replan in progress)
                if not (plan.get("status") == "failed" and worker_running):
                    # Clear any remaining spinner
                    if active_spinner_task and caps.tty:
                        sys.stdout.write(f"\r{CLEAR_LINE}")
                        sys.stdout.flush()
                    # Show token usage
                    if not quiet:
                        usage_line = render_usage(plan, caps)
                        if usage_line:
                            print(usage_line)
                    break

            # Fallback: worker stopped without creating a plan for this message
            has_matching_plan = plan and plan.get("message_id") == message_id
            if not worker_running and not has_matching_plan:
                no_plan_since_worker_stopped += 1
                # Wait a few polls to avoid race condition (worker just starting)
                if no_plan_since_worker_stopped >= 3:
                    if active_spinner_task and caps.tty:
                        sys.stdout.write(f"\r{CLEAR_LINE}")
                        sys.stdout.flush()
                    print("error: worker stopped without producing a result")
                    break
            else:
                no_plan_since_worker_stopped = 0

            # Safety net: absolute timeout
            if time.time() - start_time > _MAX_POLL_SECONDS:
                if active_spinner_task and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                print("error: timed out waiting for response")
                break

        # Animate spinner on TTY
        if caps.tty:
            frame = frames[counter % len(frames)]
            if active_spinner_task:
                line = render_task_header(
                    active_spinner_task, active_spinner_index,
                    active_spinner_total, caps, spinner_frame=frame,
                )
                sys.stdout.write(f"\r{CLEAR_LINE}{line}")
                sys.stdout.flush()
            elif planning_phase:
                line = render_planner_spinner(caps, frame)
                sys.stdout.write(f"\r{CLEAR_LINE}{line}")
                sys.stdout.flush()

        time.sleep(0.08)
        counter += 1

    return max_task_id


def _serve() -> None:
    import uvicorn

    from kiso.config import SETTINGS_DEFAULTS, load_config

    # Load config early to fail fast on errors
    cfg = load_config()
    host = cfg.settings.get("host", SETTINGS_DEFAULTS["host"])
    port = cfg.settings.get("port", SETTINGS_DEFAULTS["port"])

    uvicorn.run("kiso.main:app", host=str(host), port=int(port))


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

    elif cmd == "/clear":
        if caps.tty:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    else:
        print(f"Unknown command: {cmd}. Type /help for available commands.")


def _slash_help(caps: "TermCaps") -> None:  # noqa: F821
    """Print available REPL slash commands."""
    from kiso.render import render_separator

    print(render_separator(caps))
    print("  /help       — show this help")
    print("  /status     — server health + session info")
    print("  /sessions   — list your sessions")
    print("  /clear      — clear the screen")
    print("  /exit       — exit the REPL")
    print(render_separator(caps))


def _slash_status(client, session: str, caps: "TermCaps") -> None:  # noqa: F821
    """Show server health, session message count, and worker state."""
    import httpx

    from kiso.render import render_separator

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

    from kiso.cli_session import _relative_time
    from kiso.render import render_separator

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


if __name__ == "__main__":
    main()
