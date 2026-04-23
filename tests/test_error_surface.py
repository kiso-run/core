"""Error-surface enrichment invariants.

Encodes the contract that specific failure paths produce a user-facing
message with:
- the structured detail that identifies *what* broke (file, server,
  role, method), and
- a concrete *next step* the user can take.

Covered failure modes (the four highest-impact for pre-launch polish):

1. Malformed config TOML — the error surfaces the file path, the
   parser's line:col, and "run ``kiso doctor``" as remediation.
2. MCP transport failure during a call — the failure text names the
   server and tells the user to run ``kiso mcp test <server>``.
3. CLI ``skill install`` / ``mcp install`` missing ``--from-url`` —
   a hint points the user at ``--from-url`` instead of a raw argparse
   "required argument" message.
4. LLM timeout — the error message names the role, the model, and
   whether a fallback will be tried.

The skill-activation-miss (M1538 pre-filter) and trust-rejection
surfaces remain out of scope here — both emit debug logs already,
and the pre-filter test coverage lives with M1538.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────────
# 1. Config TOML parse error
# ────────────────────────────────────────────────────────────────────

class TestConfigTomlParseErrorEnrichment:
    def test_parse_error_names_file_and_next_step(
        self, tmp_path: Path
    ) -> None:
        from kiso.config import _build_config

        bad = tmp_path / "config.toml"
        bad.write_text("this is = not = valid = toml =\n")

        captured: list[str] = []

        def _collect(msg: str) -> None:  # simulates on_error
            captured.append(msg)
            raise SystemExit(1)

        with pytest.raises(SystemExit):
            _build_config(bad, _collect)

        assert captured, "on_error was not called"
        msg = captured[0]
        assert str(bad) in msg, (
            "TOML parse error must name the offending file"
        )
        assert "kiso doctor" in msg.lower(), (
            "TOML parse error must point the user at `kiso doctor` as "
            "the remediation command"
        )
        # The parser's location (line/col) must be surfaced too — this
        # makes the error actionable even when doctor cannot run.
        lowered = msg.lower()
        assert "line" in lowered or "at " in lowered, (
            "TOML parse error must include a line/position marker"
        )


# ────────────────────────────────────────────────────────────────────
# 2. MCP transport failure remediation hint
# ────────────────────────────────────────────────────────────────────

class TestMcpTransportFailureEnrichment:
    def test_format_has_server_name_and_test_cmd(self) -> None:
        from kiso.worker.mcp import format_mcp_transport_failure

        msg = format_mcp_transport_failure(
            server_name="github",
            method_name="list_issues",
            underlying="initialize failed: EOF on stdout",
        )
        assert "github" in msg
        assert "list_issues" in msg
        assert "initialize failed: EOF on stdout" in msg
        assert "kiso mcp test github" in msg, (
            "transport failure must point the user at `kiso mcp test "
            "<server>` as the next step"
        )


# ────────────────────────────────────────────────────────────────────
# 3. CLI install --from-url hint
# ────────────────────────────────────────────────────────────────────

class TestInstallFromUrlHint:
    @pytest.mark.parametrize(
        "argv",
        [
            ["kiso", "skill", "install", "https://github.com/a/b"],
            ["kiso", "skill", "install", "file:///tmp/x"],
            ["kiso", "mcp", "install", "npm:@foo/bar"],
            ["kiso", "mcp", "install", "pypi:foo"],
            ["kiso", "mcp", "install", "https://example.com/server.json"],
        ],
    )
    def test_bare_url_produces_hint(self, argv: list[str]) -> None:
        from cli._from_url_hint import detect_missing_from_url

        hint = detect_missing_from_url(argv)
        assert hint is not None, f"argv should trigger a hint: {argv}"
        assert "--from-url" in hint
        assert "Did you mean" in hint

    @pytest.mark.parametrize(
        "argv",
        [
            ["kiso", "skill", "install", "--from-url", "https://a"],
            ["kiso", "skill", "list"],
            ["kiso", "mcp", "install", "--from-url", "npm:x"],
            ["kiso", "mcp", "env", "foo", "set", "K", "v"],
            ["kiso", "msg", "https://example.com is a URL"],
        ],
    )
    def test_correct_usage_is_silent(self, argv: list[str]) -> None:
        from cli._from_url_hint import detect_missing_from_url

        assert detect_missing_from_url(argv) is None, (
            f"argv should NOT trigger a hint: {argv}"
        )


# ────────────────────────────────────────────────────────────────────
# 4. LLM timeout enrichment
# ────────────────────────────────────────────────────────────────────

class TestLlmTimeoutEnrichment:
    def test_format_names_role_model_and_fallback(self) -> None:
        from kiso.llm import format_llm_timeout_error

        # Primary model failed, fallback will be attempted.
        msg = format_llm_timeout_error(
            role="planner",
            model="deepseek/deepseek-v3.2",
            timeout_s=600,
            fallback_model="minimax/minimax-m2.7",
            attempt=1,
            max_attempts=3,
        )
        assert "planner" in msg
        assert "deepseek/deepseek-v3.2" in msg
        assert "600" in msg
        assert "minimax/minimax-m2.7" in msg, (
            "message must tell the user which fallback will be tried"
        )

    def test_format_handles_no_fallback(self) -> None:
        from kiso.llm import format_llm_timeout_error

        msg = format_llm_timeout_error(
            role="classifier",
            model="google/gemini-2.5-flash",
            timeout_s=30,
            fallback_model=None,
            attempt=1,
            max_attempts=1,
        )
        assert "classifier" in msg
        assert "google/gemini-2.5-flash" in msg
        assert "no fallback" in msg.lower() or "no fallback model" in msg.lower()
