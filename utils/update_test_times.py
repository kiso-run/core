#!/usr/bin/env python3
"""Update ``utils/test_times.json`` from a run_tests.sh recap log.

Usage::

    python utils/update_test_times.py <path-to-log>
    python utils/update_test_times.py -          # read log from stdin

The updater parses the ``━━━ RECAP ━━━`` block emitted by
``utils/run_tests.sh`` at the end of every run and updates the
per-tier `count` + `avg_seconds` entries used by the interactive
menu to estimate runtime. Unrelated tiers already in the JSON are
preserved on write.

The JSON schema is intentionally minimal — one entry per tier,
keyed by the human-readable suite name used in the recap::

    {
      "Unit tests":        {"count": 4379, "avg_seconds": 0.0203},
      "Bash tests":        {"count": 95,   "avg_seconds": 0.0842},
      ...
    }
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, Tuple

_RECAP_HEADER = "━━━ RECAP ━━━"
_RECAP_END = "━━━"  # recap ends at the next ━━━ line (failure summary etc.)

# Matches either pytest-style "N passed in Xs" or bats-style "N passed (Xs)".
# Captures the *numeric summary* between the suite name and the elapsed
# value — e.g. "1 failed, 37 passed, 1 skipped, 68 deselected".
# Group 1: tier name, 2: summary block, 3: elapsed seconds
_LINE_RE = re.compile(
    r"^\s*[✓✗]\s+(.+?)\s{2,}(.+?)\s+(?:in\s+([0-9.]+)s|\(([0-9.]+)s\))\s*$"
)

# Inside the summary block, pull "N <status>" tokens.
_STATUS_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|deselected|error|errors)")


def parse_recap(log: str) -> Dict[str, Tuple[int, float]]:
    """Extract per-tier (executed_count, elapsed_seconds) pairs.

    Only the block between the first ``━━━ RECAP ━━━`` line and the
    next ``━━━`` line is considered. Lines outside that block are
    ignored so that bare pytest summaries elsewhere in the log do not
    produce spurious tiers.

    Executed count excludes ``deselected`` (marker filters that never
    run) but includes ``skipped`` (they consume the collection phase).
    """
    in_block = False
    out: Dict[str, Tuple[int, float]] = {}
    for line in log.splitlines():
        stripped = line.strip()
        if not in_block:
            if _RECAP_HEADER in line:
                in_block = True
            continue
        # End of block: the next section marker (starts with ━━━)
        if stripped.startswith("━━━") and _RECAP_HEADER not in line:
            break
        m = _LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        summary = m.group(2)
        elapsed = float(m.group(3) or m.group(4))
        count = _executed_count(summary)
        if count <= 0:
            continue
        out[name] = (count, elapsed)
    return out


def _executed_count(summary: str) -> int:
    total = 0
    for m in _STATUS_RE.finditer(summary):
        n = int(m.group(1))
        status = m.group(2)
        if status == "deselected":
            continue
        total += n
    return total


def update_json_file(
    target: Path, updates: Dict[str, Tuple[int, float]]
) -> None:
    data: Dict[str, dict] = {}
    if target.exists():
        try:
            data = json.loads(target.read_text())
        except json.JSONDecodeError:
            data = {}
    for name, (count, elapsed) in updates.items():
        avg = elapsed / count if count else 0.0
        data[name] = {"count": count, "avg_seconds": round(avg, 4)}
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "-":
        log = sys.stdin.read()
    else:
        log = Path(argv[1]).read_text()
    updates = parse_recap(log)
    if not updates:
        print("no recap block found in input", file=sys.stderr)
        return 1
    target = Path(__file__).resolve().parent / "test_times.json"
    update_json_file(target, updates)
    print(f"updated {target} with {len(updates)} tier(s):")
    for name, (count, elapsed) in sorted(updates.items()):
        avg = elapsed / count if count else 0.0
        print(f"  {name:<24s} {count:>6d} tests × {avg:.4f}s = {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
