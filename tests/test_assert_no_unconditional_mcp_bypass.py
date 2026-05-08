"""M1650 — `assert_no_unconditional_mcp_bypass` helper tests.

Replaces the unconditional `assert_no_command_word` ban for tests
where the planner is allowed to fall back to exec after the
matching MCP failed in-session (per the planner.md M1609 rule).

The helper distinguishes:
- Tasks emitted BEFORE the first failed mcp call to `mcp_server`,
  or with no mcp attempt at all, that contain `capability_words`
  → that's a bypass; FAIL.
- Tasks emitted AFTER the first failed mcp call to `mcp_server`
  that contain `capability_words` → legitimate fallback; PASS.
"""

from __future__ import annotations

import pytest


from tests.functional.conftest import assert_no_unconditional_mcp_bypass


def _exec_task(command: str, status: str = "ok") -> dict:
    return {
        "type": "exec", "command": command,
        "detail": "...", "status": status,
    }


def _mcp_task(server: str, method: str, status: str = "ok") -> dict:
    return {
        "type": "mcp", "server": server, "method": method,
        "status": status,
    }


class TestUnconditionalBypass:
    """Forbidden words appearing without any MCP attempt = bypass."""

    def test_curl_with_no_mcp_attempt_is_bypass(self):
        tasks = [_exec_task("curl -sL https://example.com")]
        with pytest.raises(AssertionError):
            assert_no_unconditional_mcp_bypass(
                tasks, mcp_server="browser-mcp",
                capability_words=["curl", "wget"],
            )

    def test_curl_with_only_successful_mcp_is_bypass(self):
        """A successful MCP call doesn't authorize a curl fallback —
        the planner already got the data via the right path. Curl
        after success is suspect; forbid."""
        tasks = [
            _mcp_task("browser-mcp", "navigate", status="ok"),
            _exec_task("curl -sL https://example.com"),
        ]
        with pytest.raises(AssertionError):
            assert_no_unconditional_mcp_bypass(
                tasks, mcp_server="browser-mcp",
                capability_words=["curl", "wget"],
            )

    def test_curl_to_different_server_failure_is_bypass(self):
        """A failure on a DIFFERENT MCP doesn't authorize fallback
        for *this* MCP. The fallback rule is per-capability."""
        tasks = [
            _mcp_task("ocr-mcp", "extract_text", status="failed"),
            _exec_task("curl -sL https://example.com"),
        ]
        with pytest.raises(AssertionError):
            assert_no_unconditional_mcp_bypass(
                tasks, mcp_server="browser-mcp",
                capability_words=["curl", "wget"],
            )


class TestPostFailureFallback:
    """Forbidden words appearing AFTER a matching failed MCP =
    legitimate per M1609; helper passes."""

    def test_curl_after_failed_browser_mcp_is_allowed(self):
        tasks = [
            _mcp_task("browser-mcp", "navigate", status="failed"),
            _exec_task("curl -sL https://example.com"),
        ]
        # No assertion raised
        assert_no_unconditional_mcp_bypass(
            tasks, mcp_server="browser-mcp",
            capability_words=["curl", "wget"],
        )

    def test_multiple_post_failure_fallbacks_allowed(self):
        tasks = [
            _mcp_task("browser-mcp", "navigate", status="failed"),
            _exec_task("curl -sL https://example.com"),
            _exec_task("wget -O- https://example.com"),
        ]
        assert_no_unconditional_mcp_bypass(
            tasks, mcp_server="browser-mcp",
            capability_words=["curl", "wget"],
        )


class TestBypassBeforeFailure:
    """Forbidden word emitted BEFORE the failed MCP attempt — that's
    a parallel bypass, not a fallback. FAIL."""

    def test_curl_before_failed_mcp_is_bypass(self):
        tasks = [
            _exec_task("curl -sL https://example.com"),
            _mcp_task("browser-mcp", "navigate", status="failed"),
        ]
        with pytest.raises(AssertionError):
            assert_no_unconditional_mcp_bypass(
                tasks, mcp_server="browser-mcp",
                capability_words=["curl", "wget"],
            )


class TestEdgeCases:
    """Word boundaries + clean-empty cases preserved from the
    original assert_no_command_word."""

    def test_empty_tasks_passes(self):
        assert_no_unconditional_mcp_bypass(
            [], mcp_server="browser-mcp",
            capability_words=["curl", "wget"],
        )

    def test_word_boundaries_respected(self):
        """'curly' / 'libcurl' must NOT match 'curl'."""
        tasks = [_exec_task("apt install libcurl4-openssl-dev")]
        # No mcp at all → unconditional bypass check applies
        # (which would fail if 'curl' matched as substring) but
        # word-boundary should keep it passing.
        assert_no_unconditional_mcp_bypass(
            tasks, mcp_server="browser-mcp",
            capability_words=["curl", "wget"],
        )

    def test_only_command_field_inspected(self):
        """Like the original helper, only `command` is inspected;
        `detail` (which can contain heredoc data with substrings)
        is ignored."""
        tasks = [{
            "type": "exec",
            "command": "echo hello",
            "detail": "use curl somehow",  # contains 'curl'
            "status": "ok",
        }]
        # No mcp at all + no curl in command → must pass
        assert_no_unconditional_mcp_bypass(
            tasks, mcp_server="browser-mcp",
            capability_words=["curl", "wget"],
        )

    def test_msg_and_replan_tasks_ignored(self):
        """Non-exec tasks have no `command` field; helper skips them."""
        tasks = [
            {"type": "msg", "detail": "Use curl maybe"},
            {"type": "replan", "detail": "redo"},
            _mcp_task("browser-mcp", "navigate", status="failed"),
        ]
        assert_no_unconditional_mcp_bypass(
            tasks, mcp_server="browser-mcp",
            capability_words=["curl", "wget"],
        )
