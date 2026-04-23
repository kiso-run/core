"""Unit tests for ``MCPStdioClient``.

Uses ``tests/fixtures/mcp_mock_stdio_server.py`` as the peer. Each
test spawns the mock via the client, drives it through one or more
lifecycle phases, and asserts on the observable behavior.

The mock is controlled via ``MOCK_MCP_SCENARIO`` env — see the
fixture's docstring for the full list of scenarios.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from kiso.mcp.config import MCPServer
from kiso.mcp.schemas import (
    MCPInvocationError,
    MCPMethod,
    MCPPrompt,
    MCPPromptResult,
    MCPProtocolError,
    MCPResource,
    MCPResourceContent,
    MCPTransportError,
)
from kiso.mcp.stdio import MCPStdioClient

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_mock_stdio_server.py"


def _make_server(scenario: str = "happy", *, timeout_s: float = 10.0, **extras) -> MCPServer:
    env = {"MOCK_MCP_SCENARIO": scenario}
    env.update(extras)
    return MCPServer(
        name="mock",
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE)],
        env=env,
        cwd=None,
        enabled=True,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestInitialize:
    async def test_happy_path_returns_server_info(self):
        client = MCPStdioClient(_make_server())
        info = await client.initialize()
        assert info.name == "mock-mcp"
        assert info.version == "0.1.0"
        assert info.protocol_version == "2025-06-18"
        assert info.capabilities == {"tools": {"listChanged": False}}
        assert info.instructions == "scenario=happy"
        await client.shutdown()

    async def test_initialize_is_idempotent_error(self):
        """Calling initialize twice on the same client raises cleanly
        (does NOT spawn a second subprocess)."""
        client = MCPStdioClient(_make_server())
        await client.initialize()
        with pytest.raises(MCPProtocolError, match="already initialized"):
            await client.initialize()
        await client.shutdown()

    async def test_initialize_timeout(self):
        """slow_init scenario delays 3s; client timeout 1s → MCPTransportError."""
        client = MCPStdioClient(
            _make_server("slow_init", timeout_s=1.0, MOCK_MCP_INIT_DELAY_S="3"),
        )
        with pytest.raises(MCPTransportError):
            await client.initialize()
        await client.shutdown()

    async def test_bad_frame_handled(self):
        """bad_frame writes non-JSON before the initialize response —
        the reader must skip it and still find the valid response."""
        client = MCPStdioClient(_make_server("bad_frame"))
        info = await client.initialize()
        assert info.name == "mock-mcp"
        await client.shutdown()

    async def test_stderr_flood_does_not_block(self):
        """stderr_flood writes 256KB to stderr before responding — the
        non-blocking stderr reader must drain it without deadlocking."""
        client = MCPStdioClient(_make_server("stderr_flood", timeout_s=5.0))
        info = await client.initialize()
        assert info.name == "mock-mcp"
        # The stderr ring buffer must contain something
        assert len(client.stderr_tail()) > 0
        await client.shutdown()


# ---------------------------------------------------------------------------
# list_methods
# ---------------------------------------------------------------------------


class TestListMethods:
    async def test_happy_list(self):
        client = MCPStdioClient(_make_server())
        await client.initialize()
        methods = await client.list_methods()
        assert len(methods) == 2
        names = sorted(m.name for m in methods)
        assert names == ["add", "echo"]
        for m in methods:
            assert isinstance(m, MCPMethod)
            assert m.server == "mock"
            assert m.qualified == f"mock:{m.name}"
            assert "type" in m.input_schema
        await client.shutdown()

    async def test_pagination_follows_cursor(self):
        """large_tools_list returns 25 methods across 3 pages
        (10 + 10 + 5). The client must loop until nextCursor is None."""
        client = MCPStdioClient(_make_server("large_tools_list"))
        await client.initialize()
        methods = await client.list_methods()
        assert len(methods) == 25
        assert {m.name for m in methods} == {f"m{i}" for i in range(25)}
        await client.shutdown()

    async def test_list_before_initialize_fails(self):
        client = MCPStdioClient(_make_server())
        with pytest.raises(MCPProtocolError, match="not initialized"):
            await client.list_methods()


# ---------------------------------------------------------------------------
# call_method
# ---------------------------------------------------------------------------


class TestCallMethod:
    async def test_echo_returns_text_content(self):
        client = MCPStdioClient(_make_server())
        await client.initialize()
        result = await client.call_method("echo", {"text": "ciao"})
        assert result.is_error is False
        assert "ciao" in result.stdout_text
        await client.shutdown()

    async def test_add_returns_numeric_text(self):
        client = MCPStdioClient(_make_server())
        await client.initialize()
        result = await client.call_method("add", {"a": 2, "b": 3})
        assert result.is_error is False
        assert "5" in result.stdout_text
        await client.shutdown()

    async def test_is_error_propagated(self):
        """is_error scenario returns isError: true — client surfaces as
        MCPInvocationError with the error text in the message."""
        client = MCPStdioClient(_make_server("is_error"))
        await client.initialize()
        with pytest.raises(MCPInvocationError, match="simulated failure"):
            await client.call_method("echo", {"text": "x"})
        await client.shutdown()

    async def test_unknown_method_rpc_error(self):
        """Server returns JSON-RPC error for unknown method → raises."""
        client = MCPStdioClient(_make_server())
        await client.initialize()
        with pytest.raises(MCPInvocationError):
            await client.call_method("nonexistent", {})
        await client.shutdown()

    async def test_crash_during_call(self):
        """crash_on_call exits with code 1 on first tools/call — client
        must surface MCPTransportError, not hang."""
        client = MCPStdioClient(_make_server("crash_on_call", timeout_s=3.0))
        await client.initialize()
        with pytest.raises(MCPTransportError):
            await client.call_method("echo", {"text": "x"})
        # is_healthy returns False after the subprocess exits
        assert client.is_healthy() is False
        await client.shutdown()

    async def test_call_before_initialize_fails(self):
        client = MCPStdioClient(_make_server())
        with pytest.raises(MCPProtocolError, match="not initialized"):
            await client.call_method("echo", {"text": "x"})


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_clean_shutdown(self):
        """Happy path: close stdin, server exits cleanly."""
        client = MCPStdioClient(_make_server())
        await client.initialize()
        await client.shutdown()
        assert client.is_healthy() is False

    async def test_shutdown_idempotent(self):
        """Calling shutdown twice is safe."""
        client = MCPStdioClient(_make_server())
        await client.initialize()
        await client.shutdown()
        await client.shutdown()

    async def test_shutdown_without_initialize(self):
        """Shutdown before initialize is a no-op, not an error."""
        client = MCPStdioClient(_make_server())
        await client.shutdown()

    async def test_shutdown_sigterm_on_no_exit(self):
        """no_exit scenario ignores stdin EOF — client must escalate to
        SIGTERM and the process must exit within the grace window."""
        client = MCPStdioClient(_make_server("no_exit"))
        await client.initialize()
        # Use a very short grace to keep the test fast
        client._shutdown_grace_s = 0.3
        await client.shutdown()
        assert client.is_healthy() is False

    async def test_shutdown_sigkill_on_swallow_sigterm(self):
        """swallow_sigterm ignores SIGTERM — client escalates to SIGKILL."""
        client = MCPStdioClient(_make_server("swallow_sigterm"))
        await client.initialize()
        client._shutdown_grace_s = 0.3
        await client.shutdown()
        assert client.is_healthy() is False


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancel:
    async def test_cancel_is_fire_and_forget(self):
        """cancel() does not error even if the request id is unknown;
        it just sends a notification and returns."""
        client = MCPStdioClient(_make_server())
        await client.initialize()
        await client.cancel(12345)
        # Client still works after cancel
        result = await client.call_method("echo", {"text": "ok"})
        assert "ok" in result.stdout_text
        await client.shutdown()


# ---------------------------------------------------------------------------
# is_healthy
# ---------------------------------------------------------------------------


class TestListResources:
    async def test_happy_list(self):
        client = MCPStdioClient(_make_server("resources_happy"))
        await client.initialize()
        resources = await client.list_resources()
        assert len(resources) == 2
        assert all(isinstance(r, MCPResource) for r in resources)
        uris = {r.uri for r in resources}
        assert "kiso://logs/today" in uris
        assert "kiso://db/row/42" in uris
        by_uri = {r.uri: r for r in resources}
        assert by_uri["kiso://logs/today"].mime_type == "text/plain"
        assert by_uri["kiso://logs/today"].server == "mock"
        await client.shutdown()

    async def test_empty_when_no_resources_capability(self):
        client = MCPStdioClient(_make_server("happy"))
        await client.initialize()
        resources = await client.list_resources()
        assert resources == []
        await client.shutdown()

    async def test_pagination_follows_cursor(self):
        client = MCPStdioClient(_make_server("resources_pagination"))
        await client.initialize()
        resources = await client.list_resources()
        assert len(resources) == 25
        assert {r.uri for r in resources} == {f"kiso://gen/{i}" for i in range(25)}
        await client.shutdown()

    async def test_list_before_initialize_fails(self):
        client = MCPStdioClient(_make_server("resources_happy"))
        with pytest.raises(MCPProtocolError):
            await client.list_resources()


class TestReadResource:
    async def test_read_text_resource(self):
        client = MCPStdioClient(_make_server("resources_happy"))
        await client.initialize()
        blocks = await client.read_resource("kiso://logs/today")
        assert len(blocks) == 1
        assert isinstance(blocks[0], MCPResourceContent)
        assert blocks[0].uri == "kiso://logs/today"
        assert blocks[0].mime_type == "text/plain"
        assert blocks[0].text == "body-of:kiso://logs/today"
        assert blocks[0].blob is None
        await client.shutdown()

    async def test_read_binary_resource(self):
        client = MCPStdioClient(_make_server("resources_binary"))
        await client.initialize()
        blocks = await client.read_resource("kiso://img/logo")
        assert len(blocks) == 1
        assert blocks[0].mime_type == "image/png"
        assert blocks[0].text is None
        assert blocks[0].blob
        await client.shutdown()

    async def test_read_error_surfaces_invocation_error(self):
        client = MCPStdioClient(_make_server("resources_error"))
        await client.initialize()
        with pytest.raises(MCPInvocationError):
            await client.read_resource("kiso://missing")
        await client.shutdown()

    async def test_read_before_initialize_fails(self):
        client = MCPStdioClient(_make_server("resources_happy"))
        with pytest.raises(MCPProtocolError):
            await client.read_resource("kiso://logs/today")


class TestListPrompts:
    async def test_happy_list(self):
        client = MCPStdioClient(_make_server("prompts_happy"))
        await client.initialize()
        prompts = await client.list_prompts()
        assert len(prompts) == 2
        assert all(isinstance(p, MCPPrompt) for p in prompts)
        names = {p.name for p in prompts}
        assert names == {"code_review", "translate"}
        by_name = {p.name: p for p in prompts}
        code_review = by_name["code_review"]
        assert code_review.server == "mock"
        assert code_review.qualified == "mock:code_review"
        arg_names = {a.name for a in code_review.arguments}
        assert arg_names == {"repo", "focus"}
        required = {a.name for a in code_review.arguments if a.required}
        assert required == {"repo"}
        await client.shutdown()

    async def test_empty_when_no_prompts_capability(self):
        client = MCPStdioClient(_make_server("happy"))
        await client.initialize()
        prompts = await client.list_prompts()
        assert prompts == []
        await client.shutdown()

    async def test_pagination_follows_cursor(self):
        client = MCPStdioClient(_make_server("prompts_pagination"))
        await client.initialize()
        prompts = await client.list_prompts()
        assert len(prompts) == 25
        assert {p.name for p in prompts} == {f"prompt_{i}" for i in range(25)}
        await client.shutdown()

    async def test_list_before_initialize_fails(self):
        client = MCPStdioClient(_make_server("prompts_happy"))
        with pytest.raises(MCPProtocolError):
            await client.list_prompts()


class TestGetPrompt:
    async def test_get_rendered_prompt(self):
        client = MCPStdioClient(_make_server("prompts_happy"))
        await client.initialize()
        rendered = await client.get_prompt(
            "code_review", {"repo": "kiso-run", "focus": "mcp"},
        )
        assert isinstance(rendered, MCPPromptResult)
        assert rendered.description == "rendered:code_review"
        assert len(rendered.messages) == 1
        assert rendered.messages[0].role == "user"
        assert "Review kiso-run focusing on mcp." in rendered.messages[0].text
        await client.shutdown()

    async def test_get_error_surfaces_invocation_error(self):
        client = MCPStdioClient(_make_server("prompts_error"))
        await client.initialize()
        with pytest.raises(MCPInvocationError):
            await client.get_prompt("anything", {})
        await client.shutdown()

    async def test_get_before_initialize_fails(self):
        client = MCPStdioClient(_make_server("prompts_happy"))
        with pytest.raises(MCPProtocolError):
            await client.get_prompt("code_review", {})


class TestIsHealthy:
    async def test_before_initialize(self):
        client = MCPStdioClient(_make_server())
        assert client.is_healthy() is False

    async def test_after_initialize(self):
        client = MCPStdioClient(_make_server())
        await client.initialize()
        assert client.is_healthy() is True
        await client.shutdown()

    async def test_after_shutdown(self):
        client = MCPStdioClient(_make_server())
        await client.initialize()
        await client.shutdown()
        assert client.is_healthy() is False
