"""Token usage stats CLI command."""

from __future__ import annotations

import getpass
import sys


def run_stats_command(args) -> None:
    """Print token usage stats from the kiso server (admin only)."""
    import httpx

    from kiso.config import load_config

    cfg = load_config()
    token = cfg.tokens.get("cli")
    if not token:
        print("error: no 'cli' token in config.toml")
        sys.exit(1)

    user = getpass.getuser()
    since = getattr(args, "since", 30)
    session = getattr(args, "session", None)
    by = getattr(args, "by", "model")

    params: dict = {"user": user, "since": since, "by": by}
    if session:
        params["session"] = session

    try:
        resp = httpx.get(
            f"{args.api}/admin/stats",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        print(f"error: cannot connect to {args.api}")
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        print(f"error: {exc.response.status_code} — {exc.response.text}")
        sys.exit(1)

    print_stats(resp.json())


def print_stats(data: dict) -> None:
    """Format and print a stats response dict to stdout."""
    from kiso.stats import estimate_cost

    by = data.get("by", "model")
    since = data.get("since_days", 30)
    rows = data.get("rows", [])
    total = data.get("total", {})
    session_filter = data.get("session_filter")

    header = f"Token usage — last {since} days  (by {by})"
    if session_filter:
        header += f"  [session: {session_filter}]"
    print(header)

    if not rows:
        print("  (no data)")
        return

    costs = [estimate_cost(r) for r in rows]
    show_cost = any(c is not None for c in costs)

    # Column widths
    key_w = max(max(len(r["key"]) for r in rows), len(by))
    calls_w = max(max(len(str(r["calls"])) for r in rows), 5)
    in_w = max(max(len(_fmt_k(r["input_tokens"])) for r in rows), 7)
    out_w = max(max(len(_fmt_k(r["output_tokens"])) for r in rows), 7)

    cost_header = "  est. cost" if show_cost else ""
    sep_len = key_w + 2 + calls_w + 2 + in_w + 2 + out_w + (12 if show_cost else 0)

    print()
    print(f"  {by:<{key_w}}  {'calls':>{calls_w}}  {'input':>{in_w}}  {'output':>{out_w}}{cost_header}")
    print("  " + "─" * sep_len)

    for r, cost in zip(rows, costs):
        cost_str = f"  {_fmt_cost(cost):>10}" if show_cost else ""
        print(
            f"  {r['key']:<{key_w}}"
            f"  {r['calls']:>{calls_w}}"
            f"  {_fmt_k(r['input_tokens']):>{in_w}}"
            f"  {_fmt_k(r['output_tokens']):>{out_w}}"
            f"{cost_str}"
        )

    print("  " + "─" * sep_len)

    total_calls = total.get("calls", 0)
    total_in = total.get("input_tokens", 0)
    total_out = total.get("output_tokens", 0)
    if show_cost:
        known_costs = [c for c in costs if c is not None]
        total_cost: float | None = sum(known_costs) if known_costs else None
        total_cost_str = f"  {_fmt_cost(total_cost):>10}"
    else:
        total_cost_str = ""

    print(
        f"  {'total':<{key_w}}"
        f"  {total_calls:>{calls_w}}"
        f"  {_fmt_k(total_in):>{in_w}}"
        f"  {_fmt_k(total_out):>{out_w}}"
        f"{total_cost_str}"
    )


def _fmt_k(n: int) -> str:
    """Format token count with space as thousands separator + 'k' suffix."""
    if n < 1000:
        return str(n)
    return f"{n // 1000:,}".replace(",", " ") + " k"


def _fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "—"
    if cost == 0.0:
        return "$0.00"
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"
