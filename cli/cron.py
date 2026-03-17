"""M681: CLI commands for cron job management."""

from __future__ import annotations

import argparse
import sys

from cli._http import cli_delete, cli_get, cli_patch, cli_post


def cron_list(args: argparse.Namespace) -> None:
    """List cron jobs."""
    params: dict = {}
    if getattr(args, "session", None):
        params["session"] = args.session
    resp = cli_get(args, "/cron", params=params)
    jobs = resp.json().get("jobs", [])
    if not jobs:
        print("No cron jobs configured.")
        return
    for j in jobs:
        state = "ON" if j.get("enabled") else "OFF"
        prompt = j["prompt"]
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        print(f"  [{j['id']}] [{state}] {j['schedule']}  session={j['session']}  next={j.get('next_run', '?')}")
        print(f"         {prompt}")


def cron_add(args: argparse.Namespace) -> None:
    """Add a cron job."""
    from cli.plugin_ops import require_admin
    require_admin()

    # Client-side validation
    try:
        from croniter import croniter
        if not croniter.is_valid(args.schedule):
            print(f"error: invalid cron expression: {args.schedule}", file=sys.stderr)
            print("  Format: minute hour day month weekday", file=sys.stderr)
            print("  Examples: '0 9 * * *' (daily 9am), '*/5 * * * *' (every 5min)", file=sys.stderr)
            sys.exit(1)
    except ImportError:
        pass  # server will validate

    resp = cli_post(args, "/cron", json_body={
        "session": args.session,
        "schedule": args.schedule,
        "prompt": args.prompt,
    })
    data = resp.json()
    print(f"Cron job added (id={data['id']}): {data['schedule']} → session={data['session']}")
    print(f"  Next run: {data.get('next_run', '?')}")


def cron_remove(args: argparse.Namespace) -> None:
    """Remove a cron job by ID."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_delete(args, f"/cron/{args.job_id}")
    data = resp.json()
    if data.get("deleted"):
        print(f"Cron job {args.job_id} removed.")
    else:
        print(f"error: could not remove cron job {args.job_id}", file=sys.stderr)
        sys.exit(1)


def cron_enable(args: argparse.Namespace) -> None:
    """Enable a cron job."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_patch(args, f"/cron/{args.job_id}", params={"enabled": "true"})
    print(f"Cron job {args.job_id} enabled.")


def cron_disable(args: argparse.Namespace) -> None:
    """Disable a cron job."""
    from cli.plugin_ops import require_admin
    require_admin()
    resp = cli_patch(args, f"/cron/{args.job_id}", params={"enabled": "false"})
    print(f"Cron job {args.job_id} disabled.")
