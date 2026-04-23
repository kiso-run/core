"""Concern 8 — ``${env:VAR}`` expansion is pure string substitution.

Config values such as API keys and OAuth secrets are pulled from
the environment via ``${env:VAR_NAME}`` at parse time. The
substitution must be a literal string replacement — never a shell
evaluation, format-string interpretation, or recursive expansion —
so injecting shell metacharacters into the source env variable
cannot break out of the consuming subprocess's argv.
"""

from __future__ import annotations

import pytest

from kiso.mcp.config import MCPConfigError, parse_mcp_section


class TestEnvExpansionLiteral:
    @pytest.mark.parametrize(
        "payload",
        [
            "; rm -rf /",
            "$(whoami)",
            "`hostname`",
            "with\nnewline",
            "with; semi",
        ],
    )
    def test_shell_metacharacters_pass_through_verbatim(
        self, monkeypatch, payload
    ):
        monkeypatch.setenv("SECURITY_TEST_VAR", payload)
        parsed = parse_mcp_section({
            "s": {
                "transport": "stdio",
                "command": "tool",
                "args": ["--key", "${env:SECURITY_TEST_VAR}"],
                "env": {"WRAPPED": "${env:SECURITY_TEST_VAR}"},
            }
        })["s"]

        # The value in args and env is the raw string, no shell magic.
        assert parsed.args == ["--key", payload]
        assert parsed.env["WRAPPED"] == payload

    def test_missing_env_var_raises_not_silently_empty(self, monkeypatch):
        # An unset variable must surface as a config error, not
        # silently substitute empty string — otherwise a misconfigured
        # host could ship a subprocess with an empty auth header.
        monkeypatch.delenv("SECURITY_TEST_UNSET", raising=False)
        with pytest.raises(MCPConfigError):
            parse_mcp_section({
                "s": {
                    "transport": "stdio",
                    "command": "tool",
                    "env": {"K": "${env:SECURITY_TEST_UNSET}"},
                }
            })

    def test_expansion_is_single_pass(self, monkeypatch):
        # If the env var itself contains ``${env:...}`` it must not
        # be recursively expanded.
        monkeypatch.setenv("SECURITY_TEST_OUTER", "${env:SECURITY_TEST_INNER}")
        monkeypatch.setenv("SECURITY_TEST_INNER", "secret")
        parsed = parse_mcp_section({
            "s": {
                "transport": "stdio",
                "command": "tool",
                "env": {"K": "${env:SECURITY_TEST_OUTER}"},
            }
        })["s"]
        assert parsed.env["K"] == "${env:SECURITY_TEST_INNER}"
