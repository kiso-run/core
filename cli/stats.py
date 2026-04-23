"""Token usage stats CLI command."""

from __future__ import annotations

import getpass
import re

from cli._http import cli_get


_SINCE_RE = re.compile(r"^([1-9]\d*)(d?)$")


def parse_since(spec: str) -> int:
    """Parse a ``--since`` spec into a number of days.

    Accepts either ``7`` (integer days) or ``7d`` / ``30d`` suffix
    form. Rejects negative numbers, fractions, and unit suffixes
    other than ``d``. Raises :class:`ValueError` on bad input so the
    CLI can surface an argparse-style error.
    """
    if not isinstance(spec, str) or not spec:
        raise ValueError(f"invalid --since spec: {spec!r}")
    m = _SINCE_RE.match(spec.strip())
    if not m:
        raise ValueError(f"invalid --since spec: {spec!r}")
    return int(m.group(1))


def run_stats_command(args) -> None:
    """Print token usage stats from the kiso server (admin only)."""
    user = getattr(args, "user", None) or getpass.getuser()
    since = parse_since(str(args.since)) if not isinstance(args.since, int) else args.since
    session = args.session
    by = args.by

    params: dict = {"user": user, "since": since, "by": by}
    if session:
        params["session"] = session

    resp = cli_get(args, "/admin/stats", params=params)
    print_stats(resp.json(), costs_only=getattr(args, "costs", False))


def print_stats(data: dict, *, costs_only: bool = False) -> None:
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

    if costs_only:
        # Cost-focused view: drop token columns, keep key + calls + cost.
        key_w = max(max(len(r["key"]) for r in rows), len(by))
        calls_w = max(max(len(str(r["calls"])) for r in rows), 5)
        sep_len = key_w + 2 + calls_w + 12

        print()
        print(f"  {by:<{key_w}}  {'calls':>{calls_w}}  {'est. cost':>10}")
        print("  " + "─" * sep_len)
        for r, cost in zip(rows, costs):
            print(
                f"  {r['key']:<{key_w}}"
                f"  {r['calls']:>{calls_w}}"
                f"  {_fmt_cost(cost):>10}"
            )
        print("  " + "─" * sep_len)

        known_costs = [c for c in costs if c is not None]
        total_cost: float | None = sum(known_costs) if known_costs else None
        total_calls = total.get("calls", 0)
        print(
            f"  {'total':<{key_w}}"
            f"  {total_calls:>{calls_w}}"
            f"  {_fmt_cost(total_cost):>10}"
        )
        return

    # Column widths (full view)
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
        total_cost2: float | None = sum(known_costs) if known_costs else None
        total_cost_str = f"  {_fmt_cost(total_cost2):>10}"
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
