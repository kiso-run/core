"""CLI entry point."""

from __future__ import annotations

import argparse
import sys


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
            if text == "exit":
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
        render_msg_output,
        render_plan,
        render_review,
        render_separator,
        render_task_header,
        render_task_output,
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
            tasks = data.get("tasks", [])

            # Show plan goal / replan detection
            if plan and not quiet:
                pid = plan["id"]
                task_count = len(tasks)
                if shown_plan_id is None:
                    # Clear spinner line before printing plan
                    if active_spinner_task and caps.tty:
                        sys.stdout.write(f"\r{CLEAR_LINE}")
                        sys.stdout.flush()
                        active_spinner_task = None
                    print(f"\n{render_plan(plan['goal'], task_count, caps)}")
                    print(render_separator(caps))
                    shown_plan_id = pid
                elif pid != shown_plan_id:
                    if active_spinner_task and caps.tty:
                        sys.stdout.write(f"\r{CLEAR_LINE}")
                        sys.stdout.flush()
                        active_spinner_task = None
                    print(f"\n{render_plan(plan['goal'], task_count, caps, replan=True)}")
                    print(render_separator(caps))
                    shown_plan_id = pid
                    seen.clear()

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
                if active_spinner_task and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                    active_spinner_task = None

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

                # Print header for non-msg tasks
                print(render_task_header(task, idx, total, caps))

                # Show output for completed/failed tasks
                if status in ("done", "failed") and output:
                    out = render_task_output(output, caps)
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

            # Done condition: plan matches our message and is no longer running
            if (
                plan
                and plan.get("message_id") == message_id
                and plan.get("status") != "running"
            ):
                # Clear any remaining spinner
                if active_spinner_task and caps.tty:
                    sys.stdout.write(f"\r{CLEAR_LINE}")
                    sys.stdout.flush()
                break

            # Fallback: worker stopped without creating a plan for this message
            worker_running = data.get("worker_running", False)
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
        if active_spinner_task and caps.tty:
            frame = frames[counter % len(frames)]
            line = render_task_header(
                active_spinner_task, active_spinner_index,
                active_spinner_total, caps, spinner_frame=frame,
            )
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


if __name__ == "__main__":
    main()
