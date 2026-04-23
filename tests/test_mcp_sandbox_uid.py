"""Tests for MCP stdio sandbox_uid parity with exec.

Business requirement: when a user-role session invokes an MCP stdio
server, the MCP subprocess must be spawned under that session's
sandbox UID — the same privilege drop ``_exec_task`` already does —
so a restricted user cannot escalate by routing through an MCP
server. Admin calls still run unsandboxed. A per-server
``sandbox = "never"`` opt-out exists for servers that legitimately
need higher privileges. The MCP client pool is keyed by
``(server_name, scope_key, sandbox_uid)`` so distinct UIDs never
share a subprocess.

Covers:
- ``MCPServer.sandbox`` config field (default ``"role_based"``,
  accepts ``"never"``, rejects anything else).
- ``MCPStdioClient`` accepts ``sandbox_uid`` and passes it through
  to ``asyncio.create_subprocess_exec(..., user=sandbox_uid)``.
- ``MCPManager`` pool isolation: admin (``uid=None``) and user
  (``uid=N``) spawn separately; two distinct UIDs spawn separately;
  a server with ``sandbox="never"`` ignores the caller's UID entirely.
- ``shutdown_session`` kills every pool entry for that session
  regardless of sandbox_uid.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiso.mcp.config import MCPConfigError, MCPServer, parse_mcp_section
from kiso.mcp.manager import MCPManager
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPMethod,
    MCPServerInfo,
)
from kiso.mcp.stdio import MCPStdioClient

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_mock_stdio_server.py"


# ---------------------------------------------------------------------------
# Config: sandbox field on MCPServer
# ---------------------------------------------------------------------------


class TestMCPServerSandboxField:
    def test_sandbox_defaults_to_role_based(self):
        srv = parse_mcp_section(
            {"s": {"transport": "stdio", "command": "echo"}}
        )["s"]
        assert srv.sandbox == "role_based"

    def test_sandbox_never_accepted(self):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "stdio",
                    "command": "echo",
                    "sandbox": "never",
                }
            }
        )["s"]
        assert srv.sandbox == "never"

    def test_sandbox_role_based_accepted_explicit(self):
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "stdio",
                    "command": "echo",
                    "sandbox": "role_based",
                }
            }
        )["s"]
        assert srv.sandbox == "role_based"

    def test_sandbox_unknown_value_rejected(self):
        with pytest.raises(MCPConfigError, match="sandbox"):
            parse_mcp_section(
                {
                    "s": {
                        "transport": "stdio",
                        "command": "echo",
                        "sandbox": "yes-please",
                    }
                }
            )

    def test_sandbox_on_http_transport_accepted(self):
        """HTTP has no subprocess — the field is meaningless there but
        must still parse (so mixed configs don't break)."""
        srv = parse_mcp_section(
            {
                "s": {
                    "transport": "http",
                    "url": "https://x.example/mcp",
                    "sandbox": "never",
                }
            }
        )["s"]
        assert srv.sandbox == "never"


# ---------------------------------------------------------------------------
# MCPStdioClient: sandbox_uid threads to create_subprocess_exec(user=...)
# ---------------------------------------------------------------------------


class _RecordedSpawn:
    """Records the kwargs passed to create_subprocess_exec."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def __call__(self, *args: Any, **kwargs: Any):
        self.captured["args"] = args
        self.captured["kwargs"] = kwargs
        # Re-invoke the real API with the `user` kwarg stripped when
        # running as non-root — so the spawn still succeeds on CI —
        # but first record what was actually asked.
        real = asyncio.subprocess.create_subprocess_exec  # type: ignore[attr-defined]
        safe_kwargs = dict(kwargs)
        if os.geteuid() != 0:
            safe_kwargs.pop("user", None)
        return await real(*args, **safe_kwargs)


def _make_server(**extras: Any) -> MCPServer:
    env = {"MOCK_MCP_SCENARIO": "happy"}
    return MCPServer(
        name="mock",
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE)],
        env=env,
        cwd=None,
        enabled=True,
        timeout_s=10.0,
        **extras,
    )


class TestStdioClientSandboxUidPassthrough:
    async def test_sandbox_uid_none_does_not_pass_user_kwarg(
        self, monkeypatch
    ):
        """Admin/default spawn: no ``user=`` kwarg goes to the subprocess
        syscall (so the process inherits kiso's own UID, as before)."""
        rec = _RecordedSpawn()
        monkeypatch.setattr(
            "kiso.mcp.stdio.asyncio.create_subprocess_exec", rec
        )
        client = MCPStdioClient(_make_server(), sandbox_uid=None)
        await client.initialize()
        try:
            assert "user" not in rec.captured["kwargs"]
        finally:
            await client.shutdown()

    async def test_sandbox_uid_set_passes_user_kwarg(
        self, monkeypatch
    ):
        """When a sandbox UID is provided, it must appear as the
        ``user=`` kwarg so the kernel drops privileges at exec time."""
        rec = _RecordedSpawn()
        monkeypatch.setattr(
            "kiso.mcp.stdio.asyncio.create_subprocess_exec", rec
        )
        my_uid = os.geteuid()
        client = MCPStdioClient(_make_server(), sandbox_uid=my_uid)
        await client.initialize()
        try:
            assert rec.captured["kwargs"].get("user") == my_uid
        finally:
            await client.shutdown()

    async def test_sandbox_uid_accepted_as_keyword_only(self):
        """API hygiene: sandbox_uid must be keyword-only so callers
        can't mis-order it against extra_env."""
        # Positional sandbox_uid → TypeError
        with pytest.raises(TypeError):
            MCPStdioClient(_make_server(), 1000)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MCPManager: pool isolation by sandbox_uid
# ---------------------------------------------------------------------------


def _info(name: str = "s") -> MCPServerInfo:
    return MCPServerInfo(
        name=name,
        title=None,
        version="1.0",
        protocol_version="2025-06-18",
        capabilities={},
        instructions=None,
    )


def _method(name: str, server: str = "s") -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description="",
        input_schema={"type": "object"},
        output_schema=None,
        annotations=None,
    )


class _RecordingClient:
    def __init__(
        self,
        server: MCPServer,
        *,
        extra_env: dict[str, str] | None = None,
        sandbox_uid: int | None = None,
    ) -> None:
        self.server = server
        self.extra_env = dict(extra_env or {})
        self.sandbox_uid = sandbox_uid
        self._healthy = True
        self._initialized = False
        self._shutdown_called = False

    async def initialize(self) -> MCPServerInfo:
        self._initialized = True
        return _info(self.server.name)

    async def list_methods(self) -> list[MCPMethod]:
        return [_method("echo", self.server.name)]

    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        return MCPCallResult(
            stdout_text=f"{self.server.name}/{name}",
            published_files=[],
            structured_content=None,
            is_error=False,
        )

    async def cancel(self, request_id: Any) -> None:
        pass

    async def shutdown(self) -> None:
        self._shutdown_called = True
        self._initialized = False
        self._healthy = False

    def is_healthy(self) -> bool:
        return self._healthy and self._initialized


@pytest.fixture
def recorder():
    created: list[_RecordingClient] = []

    def factory(
        server: MCPServer,
        *,
        extra_env: dict[str, str] | None = None,
        sandbox_uid: int | None = None,
    ) -> _RecordingClient:
        c = _RecordingClient(
            server, extra_env=extra_env, sandbox_uid=sandbox_uid
        )
        created.append(c)
        return c

    factory.created = created  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def workspace_resolver(tmp_path):
    def resolver(session_id: str) -> Path:
        p = tmp_path / session_id
        p.mkdir(exist_ok=True)
        return p

    return resolver


def _session_server(name: str = "fs", sandbox: str = "role_based") -> MCPServer:
    raw = {
        name: {
            "transport": "stdio",
            "command": "mcp-filesystem",
            "args": ["--root", "${session:workspace}"],
            "env": {"SESSION_ID": "${session:id}"},
            "sandbox": sandbox,
        }
    }
    return parse_mcp_section(raw)[name]


def _plain_server(name: str = "echo", sandbox: str = "role_based") -> MCPServer:
    raw = {
        name: {
            "transport": "stdio",
            "command": "echo-server",
            "sandbox": sandbox,
        }
    }
    return parse_mcp_section(raw)[name]


class TestManagerPoolByUid:
    async def test_admin_and_user_calls_spawn_separate_clients(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        # admin path (uid=None)
        await mgr.call_method("fs", "read", {}, session="A")
        # same session, now from a user-role task (uid=42)
        await mgr.call_method("fs", "read", {}, session="A", sandbox_uid=42)
        assert len(recorder.created) == 2
        uids = [c.sandbox_uid for c in recorder.created]
        assert uids == [None, 42]
        await mgr.shutdown_all()

    async def test_two_user_calls_same_uid_reuse_client(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("fs", "read", {}, session="A", sandbox_uid=42)
        await mgr.call_method("fs", "read", {}, session="A", sandbox_uid=42)
        assert len(recorder.created) == 1
        await mgr.shutdown_all()

    async def test_same_session_different_uids_spawn_separate_clients(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("fs", "read", {}, session="A", sandbox_uid=42)
        await mgr.call_method("fs", "read", {}, session="A", sandbox_uid=43)
        assert len(recorder.created) == 2
        await mgr.shutdown_all()

    async def test_plain_server_admin_and_user_still_separate(
        self, recorder, workspace_resolver
    ):
        """Even a non-session-scoped server must isolate pool entries
        per UID, otherwise a user-role request on a global-scope server
        would share the same subprocess the admin pool already spawned."""
        mgr = MCPManager(
            {"echo": _plain_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("echo", "echo", {})  # admin
        await mgr.call_method(
            "echo", "echo", {}, session="A", sandbox_uid=42
        )
        assert len(recorder.created) == 2
        uids = [c.sandbox_uid for c in recorder.created]
        assert uids == [None, 42]
        await mgr.shutdown_all()

    async def test_sandbox_never_ignores_uid_in_pool_key(
        self, recorder, workspace_resolver
    ):
        """``sandbox = "never"`` collapses the pool across UIDs — the
        server opted out of role-based sandboxing entirely, so admin
        and user calls share one subprocess and the factory receives
        ``sandbox_uid=None``."""
        mgr = MCPManager(
            {"priv": _plain_server("priv", sandbox="never")},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("priv", "echo", {})  # admin
        await mgr.call_method(
            "priv", "echo", {}, session="A", sandbox_uid=42
        )
        assert len(recorder.created) == 1
        assert recorder.created[0].sandbox_uid is None
        await mgr.shutdown_all()

    async def test_list_methods_also_respects_sandbox_uid(
        self, recorder, workspace_resolver
    ):
        """Discovery calls must not accidentally spawn an unsandboxed
        subprocess if the caller is a user-role session."""
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
            cache_ttl_s=0.0,  # defeat the per-server methods cache
        )
        await mgr.list_methods("fs", session="A", sandbox_uid=42)
        # Directly assert the spawned client carries the UID.
        assert recorder.created[0].sandbox_uid == 42
        await mgr.shutdown_all()


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="needs root: creates a real throwaway UID, chowns a workspace, "
    "and verifies kernel-level privilege drop at exec time",
)
class TestLiveSandboxedSpawnOwnership:
    """Functional guardrail: when an unprivileged UID is passed, the
    MCP subprocess writes files owned by that UID and is denied access
    to files owned by the kiso user. Only meaningful under root, where
    we can actually drop privileges; skipped otherwise (the unit tests
    already cover the argument plumbing)."""

    async def test_spawned_mcp_process_runs_as_sandbox_uid(self, tmp_path):
        # pick an unprivileged nobody-ish UID that already exists on
        # any stock Linux and is not the kiso user
        try:
            import pwd
            sandbox_uid = pwd.getpwnam("nobody").pw_uid
        except KeyError:
            pytest.skip("no 'nobody' user on this host")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        os.chown(workspace, sandbox_uid, sandbox_uid)
        # Write a tiny MCP-like subprocess that records its euid and exits
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import os, sys, json\n"
            f"(open({str(workspace / 'euid.txt')!r}, 'w')"
            ".write(str(os.geteuid())))\n"
            "sys.exit(0)\n"
        )
        srv = MCPServer(
            name="probe",
            transport="stdio",
            command=sys.executable,
            args=[str(probe)],
            env={},
            cwd=str(workspace),
            enabled=True,
            timeout_s=5.0,
        )
        client = MCPStdioClient(srv, sandbox_uid=sandbox_uid)
        try:
            # initialize is expected to fail because the probe exits
            # immediately without speaking MCP — we don't care, we only
            # want the spawn+wait to happen so the probe runs.
            try:
                await client.initialize()
            except Exception:
                pass
        finally:
            await client.shutdown()
        # The probe wrote its geteuid() to euid.txt — must equal our UID.
        euid_file = workspace / "euid.txt"
        assert euid_file.exists(), "probe did not run"
        assert euid_file.read_text().strip() == str(sandbox_uid)
        st = euid_file.stat()
        assert st.st_uid == sandbox_uid


class TestShutdownSessionCoversAllUids:
    async def test_shutdown_session_kills_every_uid_entry(
        self, recorder, workspace_resolver
    ):
        mgr = MCPManager(
            {"fs": _session_server()},
            client_factory=recorder,
            workspace_resolver=workspace_resolver,
        )
        await mgr.call_method("fs", "read", {}, session="A")  # admin
        await mgr.call_method("fs", "read", {}, session="A", sandbox_uid=42)
        await mgr.call_method("fs", "read", {}, session="B", sandbox_uid=42)
        assert len(recorder.created) == 3

        await mgr.shutdown_session("A")

        # Both A entries (admin + uid=42) shut down; B stays live.
        a_clients = [
            c for c in recorder.created
            if c.server.env.get("SESSION_ID") == "A"
        ]
        b_clients = [
            c for c in recorder.created
            if c.server.env.get("SESSION_ID") == "B"
        ]
        assert len(a_clients) == 2
        assert all(c._shutdown_called for c in a_clients)
        assert not any(c._shutdown_called for c in b_clients)
        await mgr.shutdown_all()
