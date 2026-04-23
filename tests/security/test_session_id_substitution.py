"""Concern 6 — ``${session:*}`` substitution produces literal strings.

Session ids are user-controllable (the default format is
``hostname@user``) and flow into MCP subprocess argv and env via
``resolve_session_tokens``. Every substitution path must deliver
a literal replacement — no shell evaluation, no format-string
interpretation, no further recursive substitution.

Because ``MCPStdioClient`` spawns the subprocess with
``asyncio.create_subprocess_exec(command, *args)`` (argv list,
no shell=True), literal substitution into args is safe even for
metacharacter-heavy ids. This test pins the substitution semantics
so a future refactor cannot silently introduce shell-eval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.mcp.config import MCPServer, parse_mcp_section, resolve_session_tokens


def _server() -> MCPServer:
    return parse_mcp_section({
        "s": {
            "transport": "stdio",
            "command": "tool",
            "args": ["--id", "${session:id}", "--ws", "${session:workspace}"],
            "env": {"SID": "${session:id}"},
        }
    })["s"]


class TestLiteralSubstitution:
    @pytest.mark.parametrize(
        "dangerous_id",
        [
            "foo; rm -rf /",
            "$(malicious)",
            "`whoami`",
            "id\nnewline",
            "'single'\"double\"",
            "weird\\slash",
            "$variable",
        ],
    )
    def test_dangerous_session_id_is_literal_in_args(self, tmp_path, dangerous_id):
        out = resolve_session_tokens(_server(), dangerous_id, tmp_path)
        # The dangerous id must appear *verbatim* as a single arg
        # (no splitting, no shell evaluation, no further expansion).
        assert dangerous_id in out.args
        assert out.env["SID"] == dangerous_id

    def test_substitution_is_single_pass(self, tmp_path):
        # If a session id itself contains '${session:workspace}', it must
        # NOT trigger a second substitution — recursive expansion would
        # open a denial-of-service vector at worst and a semantic trap
        # at best.
        sneaky = "${session:workspace}"
        out = resolve_session_tokens(_server(), sneaky, tmp_path)
        # The resolved arg is literally the token string — not the
        # filesystem path it would expand to on a second pass.
        assert sneaky in out.args
        assert out.env["SID"] == sneaky
