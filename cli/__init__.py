"""CLI entry point."""

from __future__ import annotations

import argparse
import sys


class _ExitRepl(Exception):
    """Raised by /exit to break out of the REPL loop."""


_SLASH_COMMANDS = ["/clear", "/exit", "/help", "/sessions", "/status", "/verbose-off", "/verbose-on"]

_verbose_mode = False


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
    elif args.command == "serve":
        _serve()
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


def _msg_cmd(args: argparse.Namespace) -> None:
    """Send a single message and print the response (non-interactive)."""
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
        _poll_status(client, session, message_id, 0, quiet, False, caps, bot_name)
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
    from cli.render import detect_caps, render_banner, render_cancel_start, render_user_prompt

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
                    args.quiet, _verbose_mode, caps, bot_name,
                )
            except KeyboardInterrupt:
                print(f"\n{render_cancel_start(caps)}")
                try:
                    client.post(f"/sessions/{session}/cancel")
                except httpx.HTTPError:
                    pass
    finally:
        _save_readline_history()
        client.close()


_POLL_EVERY = 6  # poll API every 6 iterations (6 × 80ms ≈ 480ms)


def _poll_status(
    client: "httpx.Client",
    session: str,
    message_id: int,
    base_task_id: int,
    quiet: bool,
    verbose: bool,
    caps: "TermCaps",  # noqa: F821
    bot_name: str = "Bot",
) -> int:
    import time

    from cli.render import (
        CLEAR_LINE,
        render_command,
        render_llm_calls,
        render_llm_calls_verbose,
        render_msg_output,
        render_plan,
        render_plan_detail,
        render_planner_spinner,
        render_review,
        render_separator,
        render_step_usage,
        render_task_header,
        render_task_output,
        render_usage,
        spinner_frames,
    )

    _MAX_POLL_SECONDS = 300  # safety net: 5 min max

    seen: dict[int, tuple] = {}  # tid → (status, review_verdict, substatus, llm_call_count)
    shown_plan_id: int | None = None
    shown_plan_llm_id: int | None = None  # plan id whose llm_calls we've printed
    max_task_id = base_task_id
    counter = 0
    start_time = time.time()
    no_plan_since_worker_stopped = 0  # consecutive polls with no plan and no worker
    failed_stable_polls = 0  # polls with failed plan + all tasks rendered
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

            # Show plan-level LLM calls (planner step) once available
            if (plan and not quiet and plan.get("message_id") == message_id
                    and plan.get("llm_calls")
                    and shown_plan_llm_id != plan.get("id")):
                if (active_spinner_task or planning_phase) and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                    active_spinner_task = None
                    planning_phase = False
                llm_detail = render_llm_calls(plan.get("llm_calls"), caps)
                if llm_detail:
                    print(llm_detail)
                if verbose:
                    verbose_detail = render_llm_calls_verbose(plan.get("llm_calls"), caps)
                    if verbose_detail:
                        print(verbose_detail)
                shown_plan_llm_id = plan["id"]

            total = len(tasks)

            # Render tasks that changed status or review
            for idx, task in enumerate(tasks, 1):
                tid = task["id"]
                status = task["status"]
                review_verdict = task.get("review_verdict")
                import json as _json
                llm_call_count = len(_json.loads(task["llm_calls"])) if task.get("llm_calls") else 0
                substatus = task.get("substatus") or ""
                task_key = (status, review_verdict, substatus, llm_call_count)

                if tid > max_task_id:
                    max_task_id = tid
                if seen.get(tid) == task_key:
                    continue

                prev_key = seen.get(tid)
                seen[tid] = task_key
                prev_status = prev_key[0] if prev_key else None
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
                        # Per-call LLM breakdown first, then verbose detail
                        llm_detail = render_llm_calls(task.get("llm_calls"), caps)
                        if llm_detail:
                            print(llm_detail)
                        if verbose:
                            verbose_detail = render_llm_calls_verbose(task.get("llm_calls"), caps)
                            if verbose_detail:
                                print(verbose_detail)
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
                    active_spinner_task = task
                    active_spinner_index = idx
                    active_spinner_total = total

            # Detect planning phase: plan running/replanning, no task executing yet
            has_matching_running_plan = (
                plan
                and plan.get("message_id") == message_id
                and plan.get("status") in ("running", "replanning")
            )
            any_task_running = any(t.get("status") == "running" for t in tasks)
            planning_phase = bool(
                has_matching_running_plan and not any_task_running and not active_spinner_task
            )

            # Done condition: plan matches our message and is no longer running
            worker_running = data.get("worker_running", False)
            should_break = False
            if (
                plan
                and plan.get("message_id") == message_id
                and plan.get("status") not in ("running", "replanning")
            ):
                if plan.get("status") == "failed" and worker_running:
                    # Worker still running after plan failed — could be:
                    # a) replan in progress (new plan being created)
                    # b) post-plan processing (curator/summarizer)
                    # Wait until all tasks are rendered, then break after
                    # a short stability window to catch replans.
                    all_rendered = tasks and all(
                        seen.get(t["id"]) is not None for t in tasks
                    )
                    if all_rendered:
                        failed_stable_polls += 1
                        if failed_stable_polls >= 10:  # ~5s (10 × 480ms)
                            should_break = True
                    else:
                        failed_stable_polls = 0
                else:
                    failed_stable_polls = 0
                    should_break = True
            else:
                failed_stable_polls = 0

            if should_break:
                # Clear any remaining spinner
                if active_spinner_task and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                # Show aggregate token usage (per-call breakdown already shown per step)
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


if __name__ == "__main__":
    main()
