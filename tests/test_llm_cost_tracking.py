"""v0.10 LLM cost tracking invariants.

Cost tracking for kiso reads the audit JSONL trail (``~/.kiso/audit/``)
produced by every ``call_llm`` dispatch and aggregates tokens / cost
via ``kiso/stats.py``. The milestone requires:

1. The pricing table covers every model in ``MODEL_DEFAULTS`` — the
   roles that ship enabled out of the box must have a resolvable
   cost; missing entries are a regression.
2. ``kiso stats --since 7d`` parses the human-friendly duration form,
   not just ``--since 7`` (integer days).
3. ``kiso stats --costs`` renders only the cost view (no token
   columns) for a quick spend-focused read.
"""

from __future__ import annotations

import pytest


class TestPricingCoversDefaults:
    def test_every_default_model_has_a_price(self) -> None:
        from kiso.config import MODEL_DEFAULTS
        from kiso.stats import _find_price

        missing: list[tuple[str, str]] = []
        for role, model in MODEL_DEFAULTS.items():
            if _find_price(model) is None:
                missing.append((role, model))
        assert not missing, (
            "every role in MODEL_DEFAULTS must map to a pricing-table "
            "entry so `kiso stats --costs` can resolve cost. Missing: "
            + ", ".join(f"{r}={m}" for r, m in missing)
        )


class TestDeepseekV4Pricing:
    """V4 Flash and Pro have distinct prices and must NOT collapse to
    the generic `deepseek` fallback (which would mis-bill V4-Pro by ~6x).
    """

    def test_v4_flash_resolves_to_v4_flash_price(self) -> None:
        from kiso.stats import _find_price
        # V4-Flash: $0.14 / $0.28 per Mtok (OpenRouter list).
        assert _find_price("deepseek/deepseek-v4-flash") == (0.14, 0.28)

    def test_v4_pro_resolves_to_v4_pro_price(self) -> None:
        from kiso.stats import _find_price
        # V4-Pro: $1.74 / $3.48 per Mtok (OpenRouter list).
        assert _find_price("deepseek/deepseek-v4-pro") == (1.74, 3.48)

    def test_v4_pro_not_collapsed_to_generic_deepseek(self) -> None:
        from kiso.stats import _find_price
        # If first-match wins falls through to "deepseek" (0.14, 0.28),
        # V4-Pro billing under-counts by ~6x. Guard against it.
        price = _find_price("deepseek/deepseek-v4-pro")
        assert price != (0.14, 0.28), (
            "V4-Pro price must not fall back to the generic deepseek entry"
        )


class TestSinceDurationParser:
    @pytest.mark.parametrize(
        "spec,expected_days",
        [
            ("7", 7),
            ("7d", 7),
            ("30d", 30),
            ("1d", 1),
            ("90", 90),
        ],
    )
    def test_valid_spec(self, spec: str, expected_days: int) -> None:
        from cli.stats import parse_since

        assert parse_since(spec) == expected_days

    @pytest.mark.parametrize("spec", ["", "abc", "-1", "7x", "1.5d"])
    def test_invalid_spec_raises(self, spec: str) -> None:
        from cli.stats import parse_since

        with pytest.raises(ValueError):
            parse_since(spec)


class TestCostsOnlyFormatting:
    def test_costs_flag_shows_only_cost_column(self) -> None:
        from cli.stats import print_stats
        import io, contextlib

        data = {
            "by": "role",
            "since_days": 7,
            "rows": [
                {
                    "key": "google/gemini-2.5-flash",
                    "calls": 42,
                    "errors": 0,
                    "input_tokens": 100_000,
                    "output_tokens": 50_000,
                },
                {
                    "key": "deepseek/deepseek-v3.2",
                    "calls": 7,
                    "errors": 0,
                    "input_tokens": 20_000,
                    "output_tokens": 8_000,
                },
            ],
            "total": {
                "calls": 49,
                "input_tokens": 120_000,
                "output_tokens": 58_000,
            },
            "session_filter": None,
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_stats(data, costs_only=True)
        out = buf.getvalue()
        # Cost column must be present
        assert "$" in out or "<$" in out, "cost column must be rendered"
        # Token detail columns must NOT be shown in costs-only mode
        assert "input" not in out.lower(), (
            "--costs mode must not render the 'input' token column header"
        )
