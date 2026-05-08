"""Regression test for M1644.

`MCPStdioClient` must accept JSON-RPC response lines larger than
asyncio's default 64 KiB `StreamReader` buffer. MCP servers
routinely return image / audio / file payloads inline as a single
newline-delimited line; without an explicit `limit=` on
`create_subprocess_exec`, `stdout.readline()` raises
`LimitOverrunError("Separator is not found, and chunk exceed the
limit")`, which the client wraps as `MCPTransportError`.

Driven by the `large_tool_response` scenario in
`tests/fixtures/mcp_mock_stdio_server.py` — emits a tools/call
response with ~100 KiB of text content (over the default 64 KiB,
under the M1644 32 MiB limit).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.stdio import MCPStdioClient

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_mock_stdio_server.py"


def _make_server(scenario: str, *, timeout_s: float = 10.0) -> MCPServer:
    return MCPServer(
        name="mock",
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE)],
        env={"MOCK_MCP_SCENARIO": scenario},
        cwd=None,
        enabled=True,
        timeout_s=timeout_s,
    )


class TestLargeToolResponse:
    """`call_method` must succeed when the JSON-RPC response line is
    >64 KiB but well within reasonable MCP payload sizes."""

    async def test_response_over_default_64kib_buffer_succeeds(self):
        client = MCPStdioClient(_make_server("large_tool_response"))
        await client.initialize()
        try:
            result = await client.call_method("anything", {})
        finally:
            await client.shutdown()
        # Mock emits 100 KiB of 'x' as the tool's text content.
        assert result.stdout_text.count("x") >= 100 * 1024, (
            f"expected ~100 KiB of 'x' in result, got "
            f"{len(result.stdout_text)} chars"
        )
