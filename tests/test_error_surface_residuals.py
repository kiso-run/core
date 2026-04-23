"""Residual error-surface enrichments (M1535 follow-up).

The four sites deferred from the M1535 first pass, now landed:

5. **MCP server spawn failure** — the ``MCPTransportError`` names the
   full command line (binary + args) and, when available, the last
   few stderr lines captured before the spawn crashed.

6. **Skill activation miss** — when ``KISO_DEBUG`` is set, the
   pre-filter logs *why* a skill was rejected (``applies_to`` had no
   match in the message; an ``excludes`` pattern matched).

7. **``kiso skill install`` failure** — HTTP install errors surface
   the URL that was tried plus the remote status; bare-identifier
   mistakes produce the same "Did you mean ``--from-url``" hint as
   the MCP install path.

8. **Trust rejection** — when the install source does not match a
   trusted prefix, the error explains the expected prefix shape,
   why the supplied URL did not match, and how to add a custom
   prefix via ``kiso mcp trust add``.
"""

from __future__ import annotations

import logging

import pytest


# ────────────────────────────────────────────────────────────────────
# 5. MCP spawn failure enrichment
# ────────────────────────────────────────────────────────────────────

class TestMcpSpawnFailureEnrichment:
    def test_formatter_includes_server_command_and_args(self) -> None:
        from kiso.mcp.stdio import format_mcp_spawn_failure

        msg = format_mcp_spawn_failure(
            server_name="github",
            command="uvx",
            args=["--from", "git+https://github.com/x/y@v1", "server"],
            reason="[Errno 2] No such file or directory: 'uvx'",
            stderr_tail=None,
        )
        assert "github" in msg
        assert "uvx" in msg
        assert "git+https://github.com/x/y@v1" in msg, (
            "command args must appear so the user sees exactly what was run"
        )
        assert "No such file or directory" in msg

    def test_formatter_includes_stderr_tail_when_given(self) -> None:
        from kiso.mcp.stdio import format_mcp_spawn_failure

        stderr = "Traceback ...\n  File foo.py, line 1\nImportError: no mcp\n"
        msg = format_mcp_spawn_failure(
            server_name="broken",
            command="python",
            args=["-m", "brokenmcp"],
            reason="process exited with code 1 before initialize",
            stderr_tail=stderr,
        )
        assert "ImportError" in msg, (
            "stderr tail must be surfaced so the user sees the crash cause"
        )


# ────────────────────────────────────────────────────────────────────
# 6. Skill activation miss debug log
# ────────────────────────────────────────────────────────────────────

class TestSkillActivationMissLog:
    def test_debug_log_emitted_when_kiso_debug_set(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiso.skill_runtime import log_activation_miss

        monkeypatch.setenv("KISO_DEBUG", "1")
        caplog.set_level(logging.DEBUG, logger="kiso.skill_runtime")

        log_activation_miss(
            skill_name="code-review",
            message="list files in the repo",
            applies_to=["pull request review", "code review"],
            excludes=[],
        )

        rendered = "\n".join(r.message for r in caplog.records)
        assert "code-review" in rendered
        assert "applies_to" in rendered
        assert "code review" in rendered

    def test_debug_log_silent_without_kiso_debug(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiso.skill_runtime import log_activation_miss

        monkeypatch.delenv("KISO_DEBUG", raising=False)
        caplog.set_level(logging.DEBUG, logger="kiso.skill_runtime")

        log_activation_miss(
            skill_name="x",
            message="y",
            applies_to=["z"],
            excludes=[],
        )
        assert not caplog.records, (
            "skill activation miss must not spam logs without KISO_DEBUG"
        )


# ────────────────────────────────────────────────────────────────────
# 7. kiso skill install failure enrichment
# ────────────────────────────────────────────────────────────────────

class TestSkillInstallFailureEnrichment:
    def test_http_failure_names_url_and_status(self) -> None:
        from cli.skill import format_skill_install_http_failure

        msg = format_skill_install_http_failure(
            url="https://example.com/SKILL.md",
            status=404,
            reason="Not Found",
        )
        assert "404" in msg
        assert "https://example.com/SKILL.md" in msg
        assert "Not Found" in msg


# ────────────────────────────────────────────────────────────────────
# 8. Trust rejection enrichment
# ────────────────────────────────────────────────────────────────────

class TestTrustRejectionEnrichment:
    def test_formatter_explains_expected_prefix_and_remediation(self) -> None:
        from kiso.mcp.trust import format_trust_rejection

        msg = format_trust_rejection(
            url="https://github.com/acme/x",
            expected_prefixes=(
                "https://github.com/modelcontextprotocol/",
                "https://github.com/kiso-run/",
            ),
        )
        # Names the rejected URL and the expected shapes.
        assert "https://github.com/acme/x" in msg
        assert "https://github.com/modelcontextprotocol/" in msg
        assert "https://github.com/kiso-run/" in msg
        # Includes the remediation command.
        assert "kiso mcp trust add" in msg
