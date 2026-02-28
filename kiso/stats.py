"""Token-usage statistics — reads audit JSONL files and aggregates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Price table: maps lowercase model-name substring → (in_$/MTok, out_$/MTok).
# More specific keys must appear before less specific ones (first match wins).
# Prices are approximate and based on OpenRouter/provider pricing (early 2026).
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-flash": (0.075, 0.30),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-pro": (1.25, 5.00),
    "claude-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4": (10.00, 30.00),
    "llama-3.3": (0.20, 0.60),
    "llama-3": (0.10, 0.30),
    "mistral": (0.20, 0.60),
    "qwen": (0.10, 0.30),
}


def _find_price(model: str) -> tuple[float, float] | None:
    """Return (in_$/MTok, out_$/MTok) for *model*, or None if unknown.

    Matches by case-insensitive substring — the first match in MODEL_PRICES wins.
    """
    lower = model.lower()
    for key, prices in MODEL_PRICES.items():
        if key in lower:
            return prices
    return None


def read_audit_entries(
    audit_dir: Path,
    since: datetime | None = None,
) -> list[dict]:
    """Read LLM audit entries from JSONL files in *audit_dir*.

    Only entries with ``type == "llm"`` are returned.  If *since* is given
    (timezone-aware UTC datetime), entries older than *since* are excluded.
    Malformed JSON lines and unreadable files are silently skipped.
    """
    entries: list[dict] = []
    if not audit_dir.is_dir():
        return entries

    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "llm":
                continue
            if since is not None:
                ts_str = entry.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < since:
                        continue
                except (ValueError, TypeError):
                    pass  # malformed timestamp: include the entry to be safe
            entries.append(entry)

    return entries


def aggregate(entries: list[dict], by: str) -> list[dict]:
    """Aggregate LLM audit entries by *by* dimension.

    *by* must be one of ``"model"``, ``"session"``, or ``"role"``.
    Returns a list sorted by total tokens descending, each item having:
    ``key``, ``calls``, ``errors``, ``input_tokens``, ``output_tokens``.
    """
    groups: dict[str, dict] = {}
    for e in entries:
        key = e.get(by) or "unknown"
        if key not in groups:
            groups[key] = {
                "key": key,
                "calls": 0,
                "errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        g = groups[key]
        g["calls"] += 1
        if e.get("status") == "error":
            g["errors"] += 1
        g["input_tokens"] += e.get("input_tokens", 0)
        g["output_tokens"] += e.get("output_tokens", 0)

    return sorted(
        groups.values(),
        key=lambda r: r["input_tokens"] + r["output_tokens"],
        reverse=True,
    )


def estimate_cost(row: dict) -> float | None:
    """Estimate USD cost for a stats row.

    Looks up the model name in MODEL_PRICES (substring match on ``row["key"]``).
    Returns None if the model is not in the price table (cost unknown).
    Returns 0.0 if token counts are zero for a known model.
    """
    model = row.get("key", "")
    prices = _find_price(model)
    if prices is None:
        return None
    in_price, out_price = prices
    return (
        row.get("input_tokens", 0) * in_price
        + row.get("output_tokens", 0) * out_price
    ) / 1_000_000
